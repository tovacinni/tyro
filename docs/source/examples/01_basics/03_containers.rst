.. Comment: this file is automatically generated by `update_example_docs.py`.
   It should not be modified manually.

Containers
==========================================


Arguments of both fixed and variable lengths can be annotated with standard Python
container types: ``typing.List[T]``\ , ``typing.Tuple[T1, T2]``\ , etc. In Python >=3.9,
``list[T]`` and ``tuple[T]`` are also supported.



.. code-block:: python
        :linenos:


        import dataclasses
        import pathlib
        from typing import Tuple

        import tyro


        @dataclasses.dataclass(frozen=True)
        class TrainConfig:
            # Example of a variable-length tuple. `typing.List`, `typing.Sequence`,
            # `typing.Set`, `typing.Dict`, etc are all supported as well.
            dataset_sources: Tuple[pathlib.Path, ...]
            """Paths to load training data from. This can be multiple!"""

            # Fixed-length tuples are also okay.
            image_dimensions: Tuple[int, int] = (32, 32)
            """Height and width of some image data."""


        if __name__ == "__main__":
            config = tyro.cli(TrainConfig)
            print(config)

------------

.. raw:: html

        <kbd>python 01_basics/03_containers.py --help</kbd>

.. program-output:: python ../../examples/01_basics/03_containers.py --help

------------

.. raw:: html

        <kbd>python 01_basics/03_containers.py --dataset-sources ./data --image-dimensions 16 16</kbd>

.. program-output:: python ../../examples/01_basics/03_containers.py --dataset-sources ./data --image-dimensions 16 16

------------

.. raw:: html

        <kbd>python 01_basics/03_containers.py --dataset-sources ./data</kbd>

.. program-output:: python ../../examples/01_basics/03_containers.py --dataset-sources ./data
