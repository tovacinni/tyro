import argparse
import dataclasses
from typing import Any, Dict, List, Optional, Set, Type, TypeVar, Union

from typing_extensions import _GenericAlias  # type: ignore

from . import _arguments, _construction, _docstrings, _resolver, _strings

TypeOrGeneric = Union[Type, _GenericAlias]


@dataclasses.dataclass
class ParserDefinition:
    """Each parser contains a list of arguments and optionally a subparser."""

    description: str
    args: List["_arguments.ArgumentDefinition"]
    subparsers: Optional["SubparsersDefinition"]
    role_from_field: Dict[
        dataclasses.Field, _construction.FieldRole
    ] = dataclasses.field(default_factory=dict)

    def apply(self, parser: argparse.ArgumentParser) -> None:
        """Create defined arguments and subparsers."""

        # Put required group at start of group list
        required_group = parser.add_argument_group("required arguments")
        parser._action_groups = parser._action_groups[::-1]

        # Add each argument
        for arg in self.args:
            if arg.required:
                arg.add_argument(required_group)
            else:
                arg.add_argument(parser)

        # Add subparsers
        if self.subparsers is not None:
            subparsers = parser.add_subparsers(
                dest=_strings.SUBPARSER_DEST_FMT.format(name=self.subparsers.name),
                description=self.subparsers.description,
                required=self.subparsers.required,
            )
            for name, subparser_def in self.subparsers.parsers.items():
                subparser = subparsers.add_parser(
                    name,
                    description=subparser_def.description,
                )
                subparser_def.apply(subparser)

    @staticmethod
    def from_dataclass(
        cls: Union[Type[Any], _GenericAlias],
        parent_dataclasses: Optional[Set[Type]] = None,
        role_from_field: Optional[
            Dict[dataclasses.Field, _construction.FieldRole]
        ] = None,
        parent_type_from_typevar: Optional[Dict[TypeVar, Type]] = None,
    ) -> "ParserDefinition":
        """Create a parser definition from a dataclass."""

        if parent_dataclasses is None:
            parent_dataclasses = set()
        if role_from_field is None:
            role_from_field = {}

        assert _resolver.is_dataclass(cls)

        cls, type_from_typevar = _resolver.resolve_generic_dataclasses(cls)

        if parent_type_from_typevar is not None:
            for typevar, typ in type_from_typevar.items():
                if typ in parent_type_from_typevar:
                    type_from_typevar[typevar] = parent_type_from_typevar[typ]  # type: ignore

        assert (
            cls not in parent_dataclasses
        ), f"Found a cyclic dataclass dependency with type {cls}"

        args = []
        subparsers = None
        for field in _resolver.resolved_fields(cls):  # type: ignore
            if not field.init:
                continue

            # If set to False, we don't directly create an argument from this field
            arg_from_field: bool = True

            # Add arguments for nested dataclasses
            if _resolver.is_dataclass(field.type):
                child_definition = ParserDefinition.from_dataclass(
                    field.type,
                    parent_dataclasses | {cls},
                    role_from_field=role_from_field,
                    parent_type_from_typevar=type_from_typevar,
                )
                child_args = child_definition.args
                for i, arg in enumerate(child_args):
                    child_args[i] = dataclasses.replace(
                        arg,
                        name=field.name
                        + _strings.NESTED_DATACLASS_DELIMETER
                        + arg.name,
                    )
                args.extend(child_args)

                if child_definition.subparsers is not None:
                    assert subparsers is None
                    subparsers = child_definition.subparsers

                role_from_field[field] = _construction.FieldRole.NESTED_DATACLASS
                arg_from_field = False

            # Union of dataclasses should create subparsers
            if hasattr(field.type, "__origin__") and field.type.__origin__ is Union:
                # We don't use sets here to retain order of subcommands
                options = field.type.__args__
                options_no_none = [o for o in options if o != type(None)]  # noqa
                if len(options_no_none) >= 2 and all(
                    map(_resolver.is_dataclass, options_no_none)
                ):
                    assert (
                        subparsers is None
                    ), "Only one subparser group is supported per dataclass"

                    subparsers = SubparsersDefinition(
                        name=field.name,
                        description=_docstrings.get_field_docstring(cls, field.name),
                        parsers={
                            option.__name__: ParserDefinition.from_dataclass(
                                option,
                                parent_dataclasses | {cls},
                                role_from_field,
                                parent_type_from_typevar=type_from_typevar,
                            )
                            for option in options_no_none
                        },
                        required=(
                            options == options_no_none
                        ),  # not required if no options
                    )
                    role_from_field[field] = _construction.FieldRole.SUBPARSERS
                    arg_from_field = False

            # Make an argument!
            if arg_from_field:
                arg, role = _arguments.ArgumentDefinition.make_from_field(
                    cls, field, type_from_typevar
                )
                args.append(arg)
                role_from_field[field] = role

        return ParserDefinition(
            description=str(cls.__doc__),
            args=args,
            subparsers=subparsers,
            role_from_field=role_from_field,
        )


@dataclasses.dataclass
class SubparsersDefinition:
    """Structure for containing subparsers. Each subparser is a parser with a name."""

    name: str
    description: Optional[str]
    parsers: Dict[str, ParserDefinition]
    required: bool
