import base64

import pytest
from pydantic import ValidationError

from launchplane.product import Product
from tests.odoo_product import ATTACHMENT_BYTES, TK


def first_start_product(content: bytes = ATTACHMENT_BYTES) -> Product:
    description = TK.model_dump(mode="json")
    description["odoo"]["first_start"] = {"attachment": base64.b64encode(content).decode()}
    return Product.model_validate(description)


def test_a_description_can_supply_the_bytes_its_unattended_first_start_checks() -> None:
    product = first_start_product()
    assert product.odoo is not None and product.odoo.first_start is not None
    assert product.odoo.first_start.attachment == ATTACHMENT_BYTES
    with pytest.raises(ValidationError, match="SHA-256"):
        first_start_product(b"other bytes")
