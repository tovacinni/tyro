"""Abstractions for pulling out 'field' definitions, which specify inputs, types, and
defaults, from general callables."""
from __future__ import annotations

import collections
import collections.abc
import dataclasses
import enum
import functools
import inspect
import itertools
import typing
import warnings
from typing import (
    Any,
    Callable,
    FrozenSet,
    Hashable,
    Iterable,
    List,
    Optional,
    Tuple,
    Union,
    cast,
)

import docstring_parser
import typing_extensions
from typing_extensions import get_args, get_type_hints, is_typeddict

from . import conf  # Avoid circular import.
from . import _docstrings, _instantiators, _resolver, _singleton, _strings
from ._typing import TypeForm
from .conf import _confstruct, _markers


@dataclasses.dataclass(frozen=True)
class FieldDefinition:
    name: str
    typ: TypeForm[Any]
    default: Any
    helptext: Optional[str]
    markers: FrozenSet[_markers._Marker]

    argconf: _confstruct._ArgConfiguration

    # Override the name in our kwargs. Useful whenever the user-facing argument name
    # doesn't match the keyword expected by our callable.
    call_argname: Any

    def __post_init__(self):
        if (
            _markers.Fixed in self.markers or _markers.Suppress in self.markers
        ) and self.default in MISSING_SINGLETONS:
            raise _instantiators.UnsupportedTypeAnnotationError(
                f"Field {self.name} is missing a default value!"
            )

    @staticmethod
    def make(
        name: str,
        typ: TypeForm[Any],
        default: Any,
        helptext: Optional[str],
        call_argname_override: Optional[Any] = None,
        *,
        markers: Tuple[_markers._Marker, ...] = (),
    ):
        # Try to extract argconf overrides from type.
        _, argconfs = _resolver.unwrap_annotated(typ, _confstruct._ArgConfiguration)
        if len(argconfs) == 0:
            argconf = _confstruct._ArgConfiguration(None, None, None)
        else:
            assert len(argconfs) == 1
            (argconf,) = argconfs
            helptext = argconf.help

        typ, inferred_markers = _resolver.unwrap_annotated(typ, _markers._Marker)
        return FieldDefinition(
            name if argconf.name is None else argconf.name,
            typ,
            default,
            helptext,
            frozenset(inferred_markers).union(markers),
            argconf,
            call_argname_override if call_argname_override is not None else name,
        )

    def add_markers(self, markers: Tuple[_markers._Marker, ...]) -> FieldDefinition:
        return dataclasses.replace(
            self,
            markers=self.markers.union(markers),
        )

    def is_positional(self) -> bool:
        """Returns True if the argument should be positional in the commandline."""
        return (
            # Explicit positionals.
            _markers.Positional in self.markers
            # Dummy dataclasses should have a single positional field.
            or self.name == _strings.dummy_field_name
        )

    def is_positional_call(self) -> bool:
        """Returns True if the argument should be positional in underlying Python call."""
        return (
            # Explicit positionals.
            _markers._PositionalCall in self.markers
            # Dummy dataclasses should have a single positional field.
            or self.name == _strings.dummy_field_name
        )


class PropagatingMissingType(_singleton.Singleton):
    pass


class NonpropagatingMissingType(_singleton.Singleton):
    pass


class ExcludeFromCallType(_singleton.Singleton):
    pass


# We have two types of missing sentinels: a propagating missing value, which when set as
# a default will set all child values of nested structures as missing as well, and a
# nonpropagating missing sentinel, which does not override child defaults.
MISSING_PROP = PropagatingMissingType()
MISSING_NONPROP = NonpropagatingMissingType()

# When total=False in a TypedDict, we exclude fields from the constructor by default.
EXCLUDE_FROM_CALL = ExcludeFromCallType()

# Note that our "public" missing API will always be the propagating missing sentinel.
MISSING_PUBLIC: Any = MISSING_PROP
"""Sentinel value to mark fields as missing. Can be used to mark fields passed in as a
`default_instance` for `tyro.cli()` as required."""


MISSING_SINGLETONS = [
    dataclasses.MISSING,
    MISSING_PROP,
    MISSING_NONPROP,
    inspect.Parameter.empty,
]
try:
    # Undocumented feature: support omegaconf dataclasses out of the box.
    import omegaconf

    MISSING_SINGLETONS.append(omegaconf.MISSING)
except ImportError:
    pass


@dataclasses.dataclass(frozen=True)
class UnsupportedNestedTypeMessage:
    """Reason why a callable cannot be treated as a nested type."""

    message: str


def is_nested_type(typ: TypeForm[Any], default_instance: _DefaultInstance) -> bool:
    """Determine whether a type should be treated as a 'nested type', where a single
    type can be broken down into multiple fields (eg for nested dataclasses or
    classes)."""
    return not isinstance(
        _try_field_list_from_callable(typ, default_instance),
        UnsupportedNestedTypeMessage,
    )


def field_list_from_callable(
    f: Union[Callable, TypeForm[Any]],
    default_instance: _DefaultInstance,
) -> List[FieldDefinition]:
    """Generate a list of generic 'field' objects corresponding to the inputs of some
    annotated callable."""
    out = _try_field_list_from_callable(f, default_instance)

    if isinstance(out, UnsupportedNestedTypeMessage):
        raise _instantiators.UnsupportedTypeAnnotationError(out.message)

    # Recursively apply markers.
    _, parent_markers = _resolver.unwrap_annotated(f, _markers._Marker)
    out = list(map(lambda field: field.add_markers(parent_markers), out))
    return out


# Implementation details below.


_DefaultInstance = Union[
    Any, PropagatingMissingType, NonpropagatingMissingType, ExcludeFromCallType
]

_known_parsable_types = set(
    filter(
        lambda x: isinstance(x, Hashable),  # type: ignore
        itertools.chain(
            __builtins__.values(),  # type: ignore
            vars(typing).values(),
            vars(typing_extensions).values(),
            vars(collections.abc).values(),
        ),
    )
)


def _try_field_list_from_callable(
    f: Union[Callable, TypeForm[Any]],
    default_instance: _DefaultInstance,
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    f, found_subcommand_configs = _resolver.unwrap_annotated(
        f, conf._confstruct._SubcommandConfiguration
    )
    if len(found_subcommand_configs) > 0:
        default_instance = found_subcommand_configs[0].default

    # Unwrap generics.
    f, type_from_typevar = _resolver.resolve_generic_types(f)
    f = _resolver.narrow_type(f, default_instance)
    f_origin = _resolver.unwrap_origin_strip_extras(cast(TypeForm, f))

    # If `f` is a type:
    #     1. Set cls to the type.
    #     2. Consider `f` to be `cls.__init__`.
    cls: Optional[TypeForm[Any]] = None
    if isinstance(f, type):
        cls = f
        f = cls.__init__  # type: ignore
        f_origin = cls  # type: ignore

    # Try field generation from class inputs.
    if cls is not None:
        for match, field_list_from_class in (
            (is_typeddict, _field_list_from_typeddict),
            (_resolver.is_namedtuple, _field_list_from_namedtuple),
            (_resolver.is_dataclass, _field_list_from_dataclass),
            (_is_attrs, _field_list_from_attrs),
            (_is_pydantic, _field_list_from_pydantic),
        ):
            if match(cls):
                return field_list_from_class(cls, default_instance)

    # Standard container types. These are different because they can be nested structures
    # if they contain other nested types (eg Tuple[Struct, Struct]), or treated as
    # single arguments otherwise (eg Tuple[int, int]).
    #
    # Note that f_origin will be populated if we annotate as `Tuple[..]`, and cls will
    # be populated if we annotate as just `tuple`.
    if f_origin is tuple or cls is tuple:
        return _field_list_from_tuple(f, default_instance)
    elif f_origin in (collections.abc.Mapping, dict) or cls in (
        collections.abc.Mapping,
        dict,
    ):
        return _field_list_from_dict(f, default_instance)
    elif f_origin in (list, set, typing.Sequence) or cls in (
        list,
        set,
        typing.Sequence,
    ):
        return _field_list_from_sequence_checked(f, default_instance)

    # General cases.
    if (
        cls is not None and cls in _known_parsable_types
    ) or _resolver.unwrap_origin_strip_extras(f) in _known_parsable_types:
        return UnsupportedNestedTypeMessage(f"{f} should be parsed directly!")
    else:
        return _try_field_list_from_general_callable(f, cls, default_instance)


def _field_list_from_typeddict(
    cls: TypeForm[Any], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    field_list = []
    valid_default_instance = (
        default_instance not in MISSING_SINGLETONS
        and default_instance is not EXCLUDE_FROM_CALL
    )
    assert not valid_default_instance or isinstance(default_instance, dict)
    for name, typ in get_type_hints(cls, include_extras=True).items():
        if valid_default_instance:
            default = default_instance.get(name, MISSING_PROP)  # type: ignore
        elif getattr(cls, "__total__") is False:
            default = EXCLUDE_FROM_CALL
            if is_nested_type(typ, MISSING_NONPROP):
                raise _instantiators.UnsupportedTypeAnnotationError(
                    "`total=False` not supported for nested structures."
                )
        else:
            default = MISSING_PROP

        field_list.append(
            FieldDefinition.make(
                name=name,
                typ=typ,
                default=default,
                helptext=_docstrings.get_field_docstring(cls, name),
            )
        )
    return field_list


def _field_list_from_namedtuple(
    cls: TypeForm[Any], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # Handle NamedTuples.
    #
    # TODO: in terms of helptext, we currently do display the default NamedTuple
    # helptext. But we (intentionally) don't for dataclasses; this is somewhat
    # inconsistent.
    field_list = []
    field_defaults = getattr(cls, "_field_defaults")

    # Note that _field_types is removed in Python 3.9.
    for name, typ in get_type_hints(cls, include_extras=True).items():
        # Get default, with priority for `default_instance`.
        default = field_defaults.get(name, MISSING_NONPROP)
        if hasattr(default_instance, name):
            default = getattr(default_instance, name)
        if default_instance is MISSING_PROP:
            default = MISSING_PROP

        field_list.append(
            FieldDefinition.make(
                name=name,
                typ=typ,
                default=default,
                helptext=_docstrings.get_field_docstring(cls, name),
            )
        )
    return field_list


def _field_list_from_dataclass(
    cls: TypeForm[Any], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # Handle dataclasses.
    field_list = []
    for dc_field in filter(lambda field: field.init, _resolver.resolved_fields(cls)):
        default = _get_dataclass_field_default(dc_field, default_instance)

        # Try to get helptext from field metadata. This is also intended to be
        # compatible with HuggingFace-style config objects.
        helptext = dc_field.metadata.get("help", None)
        assert isinstance(helptext, (str, type(None)))

        # Try to get helptext from docstrings. Note that this can't be generated
        # dynamically.
        if helptext is None:
            helptext = _docstrings.get_field_docstring(cls, dc_field.name)

        field_list.append(
            FieldDefinition.make(
                name=dc_field.name,
                typ=dc_field.type,
                default=default,
                helptext=helptext,
            )
        )
    return field_list


# Support attrs and pydantic if they're installed.

try:
    import pydantic
except ImportError:
    pydantic = None  # type: ignore


def _is_pydantic(cls: TypeForm[Any]) -> bool:
    return pydantic is not None and issubclass(cls, pydantic.BaseModel)


def _field_list_from_pydantic(
    cls: TypeForm[Any], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    assert pydantic is not None

    # Handle pydantic models.
    field_list = []
    for pd_field in cls.__fields__.values():  # type: ignore
        field_list.append(
            FieldDefinition.make(
                name=pd_field.name,
                typ=pd_field.outer_type_,
                default=MISSING_NONPROP
                if pd_field.required
                else pd_field.get_default(),
                helptext=pd_field.field_info.description,
            )
        )
    return field_list


try:
    import attr
except ImportError:
    attr = None  # type: ignore


def _is_attrs(cls: TypeForm[Any]) -> bool:
    return attr is not None and attr.has(cls)


def _field_list_from_attrs(
    cls: TypeForm[Any], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    assert attr is not None

    # Handle attr classes.
    field_list = []
    for attr_field in attr.fields(cls):
        # Default handling.
        default = attr_field.default
        if default is attr.NOTHING:
            default = MISSING_NONPROP
        elif isinstance(default, attr.Factory):  # type: ignore
            default = default.factory()  # type: ignore

        assert attr_field.type is not None
        field_list.append(
            FieldDefinition.make(
                name=attr_field.name,
                typ=attr_field.type,
                default=default,
                helptext=_docstrings.get_field_docstring(cls, attr_field.name),
            )
        )
    return field_list


def _field_list_from_tuple(
    f: Union[Callable, TypeForm[Any]], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # Fixed-length tuples.
    field_list = []
    children = get_args(f)
    if Ellipsis in children:
        return _try_field_list_from_sequence_inner(
            next(iter(set(children) - {Ellipsis})), default_instance
        )

    # Infer more specific type when tuple annotation isn't subscripted. This generally
    # doesn't happen
    if len(children) == 0:
        if default_instance in MISSING_SINGLETONS:
            raise _instantiators.UnsupportedTypeAnnotationError(
                "If contained types of a tuple are not specified in the annotation, a"
                " default instance must be specified."
            )
        else:
            assert isinstance(default_instance, tuple)
            children = tuple(type(x) for x in default_instance)

    if (
        default_instance in MISSING_SINGLETONS
        # EXCLUDE_FROM_CALL indicates we're inside a TypedDict, with total=False.
        or default_instance is EXCLUDE_FROM_CALL
    ):
        default_instance = (default_instance,) * len(children)

    for i, child in enumerate(children):
        default_i = default_instance[i]  # type: ignore
        field_list.append(
            FieldDefinition.make(
                # Ideally we'd have --tuple[0] instead of --tuple.0 as the command-line
                # argument, but in practice the brackets are annoying because they
                # require escaping.
                name=str(i),
                typ=child,
                default=default_i,
                helptext="",
                # This should really set the positional marker, but the CLI is more
                # intuitive for mixed nested/non-nested types in tuples when we stick
                # with kwargs. Tuples are special-cased in _calling.py.
            )
        )

    contains_nested = False
    for field in field_list:
        contains_nested |= is_nested_type(field.typ, field.default)
    if not contains_nested:
        # We could also check for variable length children, which can be populated when
        # the tuple is interpreted as a nested field but not a directly parsed one.
        return UnsupportedNestedTypeMessage(
            "Tuple does not contain any nested structures."
        )

    return field_list


def _field_list_from_sequence_checked(
    f: Union[Callable, TypeForm[Any]], default_instance: _DefaultInstance
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    contained_type: Any
    if len(get_args(f)) == 0:
        if default_instance in MISSING_SINGLETONS:
            raise _instantiators.UnsupportedTypeAnnotationError(
                f"Sequence type {f} needs either an explicit type or a"
                " default to infer from."
            )
        assert isinstance(default_instance, Iterable)
        contained_type = next(iter(default_instance))
    else:
        (contained_type,) = get_args(f)
    return _try_field_list_from_sequence_inner(contained_type, default_instance)


def _try_field_list_from_sequence_inner(
    contained_type: TypeForm[Any],
    default_instance: _DefaultInstance,
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # When no default instance is specified:
    #     If we have List[int] => this can be parsed as a single field.
    #     If we have List[SomeStruct] => OK.
    if default_instance in MISSING_SINGLETONS and not is_nested_type(
        contained_type, MISSING_NONPROP
    ):
        return UnsupportedNestedTypeMessage(
            f"Sequence containing type {contained_type} should be parsed directly!"
        )

    # If we have a default instance:
    #     [int, int, int] => this can be parsed as a single field.
    #     [SomeStruct, int, int] => OK.
    if isinstance(default_instance, Iterable) and all(
        [not is_nested_type(type(x), x) for x in default_instance]
    ):
        return UnsupportedNestedTypeMessage(
            f"Sequence with default {default_instance} should be parsed directly!"
        )
    if default_instance in MISSING_SINGLETONS:
        # We use the broader error type to prevent it from being caught by
        # is_possibly_nested_type(). This is for sure a bad annotation!
        raise _instantiators.UnsupportedTypeAnnotationError(
            "For variable-length sequences over nested types, we need a default value"
            " to infer length from."
        )

    field_list = []
    for i, default_i in enumerate(default_instance):  # type: ignore
        field_list.append(
            FieldDefinition.make(
                name=str(i),
                typ=contained_type,
                default=default_i,
                helptext="",
            )
        )
    return field_list


def _field_list_from_dict(
    f: Union[Callable, TypeForm[Any]],
    default_instance: _DefaultInstance,
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    if default_instance in MISSING_SINGLETONS:
        return UnsupportedNestedTypeMessage(
            "Nested dictionary structures must have a default instance specified."
        )
    field_list = []
    for k, v in cast(dict, default_instance).items():
        field_list.append(
            FieldDefinition.make(
                name=str(k) if not isinstance(k, enum.Enum) else k.name,
                typ=type(v),
                default=v,
                helptext=None,
                # Dictionary specific key:
                call_argname_override=k,
            )
        )
    return field_list


def _try_field_list_from_general_callable(
    f: Union[Callable, TypeForm[Any]],
    cls: Optional[TypeForm[Any]],
    default_instance: _DefaultInstance,
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # Handle general callables.
    if default_instance not in MISSING_SINGLETONS:
        return UnsupportedNestedTypeMessage(
            "`default_instance` is supported only for select types:"
            " dataclasses, lists, NamedTuple, TypedDict, etc."
        )

    # Generate field list from function signature.
    if not callable(f):
        return UnsupportedNestedTypeMessage(
            f"Cannot extract annotations from {f}, which is not a callable type."
        )
    params = list(inspect.signature(f).parameters.values())
    if cls is not None:
        # Ignore self parameter.
        params = params[1:]

    out = _field_list_from_params(f, cls, params)
    if not isinstance(out, UnsupportedNestedTypeMessage):
        return out

    # Return error message.
    assert isinstance(out, UnsupportedNestedTypeMessage)
    return out


def _field_list_from_params(
    f: Union[Callable, TypeForm[Any]],
    cls: Optional[TypeForm[Any]],
    params: List[inspect.Parameter],
) -> Union[List[FieldDefinition], UnsupportedNestedTypeMessage]:
    # Unwrap functools.wraps and functools.partial.
    done = False
    while not done:
        done = True
        if hasattr(f, "__wrapped__"):
            f = f.__wrapped__
            done = False
        if isinstance(f, functools.partial):
            f = f.func
            done = False

    # Sometime functools.* is applied to a class.
    if isinstance(f, type):
        cls = f
        f = f.__init__  # type: ignore

    # Get type annotations, docstrings.
    docstring = inspect.getdoc(f)
    docstring_from_arg_name = {}
    if docstring is not None:
        for param_doc in docstring_parser.parse(docstring).params:
            docstring_from_arg_name[param_doc.arg_name] = param_doc.description
    del docstring

    # This will throw a type error for torch.device, typing.Dict, etc.
    try:
        hints = get_type_hints(f, include_extras=True)
    except TypeError:
        return UnsupportedNestedTypeMessage(f"Could not get hints for {f}!")

    field_list = []
    for param in params:
        # Get default value.
        default = param.default

        # Get helptext from docstring.
        helptext = docstring_from_arg_name.get(param.name)
        if helptext is None and cls is not None:
            helptext = _docstrings.get_field_docstring(cls, param.name)

        if param.name not in hints:
            out = UnsupportedNestedTypeMessage(
                f"Expected fully type-annotated callable, but {f} with arguments"
                f" {tuple(map(lambda p: p.name, params))} has no annotation for"
                f" '{param.name}'."
            )
            if param.kind is param.KEYWORD_ONLY:
                # If keyword only: this can't possibly be an instantiator function
                # either, so we escalate to an error.
                raise _instantiators.UnsupportedTypeAnnotationError(out.message)
            return out

        field_list.append(
            FieldDefinition.make(
                name=param.name,
                # Note that param.annotation doesn't resolve forward references.
                typ=hints[param.name],
                default=default,
                helptext=helptext,
                markers=(_markers.Positional, _markers._PositionalCall)
                if param.kind is inspect.Parameter.POSITIONAL_ONLY
                else (),
            )
        )

    return field_list


def _ensure_dataclass_instance_used_as_default_is_frozen(
    field: dataclasses.Field, default_instance: Any
) -> None:
    """Ensure that a dataclass type used directly as a default value is marked as
    frozen."""
    assert dataclasses.is_dataclass(default_instance)
    cls = type(default_instance)
    if not cls.__dataclass_params__.frozen:  # type: ignore
        warnings.warn(
            f"Mutable type {cls} is used as a default value for `{field.name}`. This is"
            " dangerous! Consider using `dataclasses.field(default_factory=...)` or"
            f" marking {cls} as frozen."
        )


def _get_dataclass_field_default(
    field: dataclasses.Field, parent_default_instance: Any
) -> Optional[Any]:
    """Helper for getting the default instance for a field."""
    # If the dataclass's parent is explicitly marked MISSING, mark this field as missing
    # as well.
    if parent_default_instance is MISSING_PROP:
        return MISSING_PROP

    # Try grabbing default from parent instance.
    if (
        parent_default_instance not in MISSING_SINGLETONS
        and parent_default_instance is not None
    ):
        # Populate default from some parent, eg `default_instance` in `tyro.cli()`.
        if hasattr(parent_default_instance, field.name):
            return getattr(parent_default_instance, field.name)
        else:
            warnings.warn(
                f"Could not find field {field.name} in default instance"
                f" {parent_default_instance}, which has"
                f" type {type(parent_default_instance)},",
                stacklevel=2,
            )

    # Try grabbing default from dataclass field.
    if field.default not in MISSING_SINGLETONS:
        default = field.default
        # Note that dataclasses.is_dataclass() will also return true for dataclass
        # _types_, not just instances.
        if type(default) is not type and dataclasses.is_dataclass(default):
            _ensure_dataclass_instance_used_as_default_is_frozen(field, default)
        return default

    # Populate default from `dataclasses.field(default_factory=...)`.
    if field.default_factory is not dataclasses.MISSING and not (
        # Special case to ignore default_factory if we write:
        # `field: Dataclass = dataclasses.field(default_factory=Dataclass)`.
        #
        # In other words, treat it the same way as: `field: Dataclass`.
        #
        # The only time this matters is when we our dataclass has a `__post_init__`
        # function that mutates the dataclass. We choose here to use the default values
        # before this method is called.
        dataclasses.is_dataclass(field.type)
        and field.default_factory is field.type
    ):
        return field.default_factory()

    # Otherwise, no default. This is different from MISSING, because MISSING propagates
    # to children. We could revisit this design to make it clearer.
    return MISSING_NONPROP
