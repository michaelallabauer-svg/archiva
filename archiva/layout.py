"""
Layout Engine for automatic form rendering.

Takes metadata field definitions and generates an optimized
barrier-free layout with intelligent row distribution.
"""

from typing import Any

from archiva.schema import (
    DisplayWidth,
    GeneratedLayout,
    LayoutColumn,
    LayoutRow,
    MetadataFieldResponse,
)


# Width multipliers for calculating space usage
WIDTH_MAP = {
    DisplayWidth.FULL: 1.0,
    DisplayWidth.HALF: 0.5,
    DisplayWidth.THIRD: 0.333,
    DisplayWidth.QUARTER: 0.25,
}

# Default widths by field type (sensible defaults)
TYPE_DEFAULTS: dict[str, tuple[DisplayWidth, int]] = {
    "text": (DisplayWidth.HALF, 1),
    "number": (DisplayWidth.HALF, 1),
    "currency": (DisplayWidth.HALF, 1),
    "date": (DisplayWidth.HALF, 1),
    "datetime": (DisplayWidth.HALF, 1),
    "selection": (DisplayWidth.HALF, 1),
    "multi_selection": (DisplayWidth.FULL, 1),
    "boolean": (DisplayWidth.QUARTER, 1),
    "long_text": (DisplayWidth.FULL, 1),
    "url": (DisplayWidth.HALF, 1),
    "email": (DisplayWidth.HALF, 1),
    "phone": (DisplayWidth.HALF, 1),
}

# Row capacity (in "units" where full = 1.0)
ROW_CAPACITY = 1.0


def _estimate_width(field: MetadataFieldResponse) -> float:
    """Estimate the display width of a field."""
    if field.width:
        return WIDTH_MAP.get(field.width, 0.5)
    # Fallback to type default
    default = TYPE_DEFAULTS.get(field.field_type, (DisplayWidth.HALF, 1))
    return WIDTH_MAP.get(default[0], 0.5)


def _is_full_width(field: MetadataFieldResponse) -> bool:
    """Check if a field should always take full width."""
    return field.field_type in ("long_text", "multi_selection")


def _get_field_priority(field: MetadataFieldResponse) -> tuple[int, int]:
    """
    Calculate sort priority for a field.

    Returns (priority, order) where:
    - Lower priority number = higher in form (0 = top)
    - Order is the explicit order value
    """
    priority = field.order

    # Required fields come first
    if field.is_required:
        priority -= 100

    # Full-width fields (long_text) go at the bottom
    if _is_full_width(field):
        priority += 1000

    return (priority, field.order)


def generate_layout(
    fields: list[MetadataFieldResponse],
    document_type_id: str,
    document_type_name: str,
) -> GeneratedLayout:
    """
    Generate an optimized form layout from field definitions.

    Algorithm:
    1. Sort fields by priority (required first, then by order)
    2. Distribute fields across rows with width-based fitting
    3. Full-width fields always get their own row
    4. Adjacent fields that fit together are grouped

    The layout engine respects:
    - Field width preferences
    - Required vs optional ordering
    - Type-appropriate defaults
    """
    if not fields:
        return GeneratedLayout(
            document_type_id=document_type_id,
            document_type_name=document_type_name,
            rows=[],
            total_fields=0,
        )

    # Sort by priority
    sorted_fields = sorted(fields, key=_get_field_priority)

    rows: list[LayoutRow] = []
    current_row: list[LayoutColumn] = []
    current_capacity = ROW_CAPACITY
    row_order = 0

    for field in sorted_fields:
        estimated_width = _estimate_width(field)

        # Handle full-width fields
        if _is_full_width(field):
            # Flush current row first
            if current_row:
                rows.append(LayoutRow(order=row_order, columns=current_row))
                row_order += 1
                current_row = []
                current_capacity = ROW_CAPACITY

            # Add full-width as its own row
            rows.append(
                LayoutRow(
                    order=row_order,
                    columns=[LayoutColumn(field=field, width=DisplayWidth.FULL)],
                )
            )
            row_order += 1
            continue

        # Check if field fits in current row
        if estimated_width <= current_capacity:
            current_row.append(LayoutColumn(field=field, width=field.width or DisplayWidth.HALF))
            current_capacity -= estimated_width
        else:
            # Start new row
            if current_row:
                rows.append(LayoutRow(order=row_order, columns=current_row))
                row_order += 1

            current_row = [LayoutColumn(field=field, width=field.width or DisplayWidth.HALF)]
            current_capacity = ROW_CAPACITY - estimated_width

    # Flush remaining row
    if current_row:
        rows.append(LayoutRow(order=row_order, columns=current_row))

    return GeneratedLayout(
        document_type_id=document_type_id,
        document_type_name=document_type_name,
        rows=rows,
        total_fields=len(fields),
    )


def get_field_html_attributes(field: MetadataFieldResponse) -> dict[str, Any]:
    """
    Generate HTML/ARIA attributes for a field to ensure accessibility.

    Returns a dict suitable for spreading onto a form input element.
    """
    attrs: dict[str, Any] = {
        "id": f"field-{field.id}",
        "name": field.name,
        "aria-describedby": f"desc-{field.id}" if field.description else None,
        "aria-required": field.is_required,
    }

    # Type-specific attributes
    if field.field_type == "number":
        if field.min_value is not None:
            attrs["min"] = field.min_value
        if field.max_value is not None:
            attrs["max"] = field.max_value

    elif field.field_type == "text":
        if field.min_length is not None:
            attrs["minlength"] = field.min_length
        if field.max_length is not None:
            attrs["maxlength"] = field.max_length

    elif field.field_type == "date":
        if field.min_value:
            attrs["min"] = field.min_value
        if field.max_value:
            attrs["max"] = field.max_value

    elif field.field_type == "url":
        attrs["type"] = "url"

    elif field.field_type == "email":
        attrs["type"] = "email"

    elif field.field_type == "phone":
        attrs["type"] = "tel"

    return {k: v for k, v in attrs.items() if v is not None}
