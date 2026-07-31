"""Duplicate-rejecting safe YAML loading for authoritative documents."""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]


class UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """SafeLoader variant that refuses ambiguous last-value-wins mappings."""


def _construct_unique_mapping(
    loader: UniqueKeySafeLoader, node: Any, deep: bool = False
) -> dict[object, object]:
    loader.flatten_mapping(node)
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from error
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found a duplicate mapping key",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_unique_yaml(document: str) -> Any:
    """Parse YAML with SafeLoader semantics and strict mapping uniqueness."""
    return yaml.load(document, Loader=UniqueKeySafeLoader)
