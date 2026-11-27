import base64

from launchplane.odoo import FirstStart, Odoo
from launchplane.product import Database, Product
from tests.recorded_git_host import IMAGE, REPOSITORY, WORKFLOW

ADDONS_PATH = ["/opt/project/addons", "/opt/project/addons/shared", "/opt/launchplane/addons", "/odoo/addons", "/odoo/odoo/addons"]
# As TK's manifest for the recorded run listed them.
MODULES = [
    "launchplane_settings",
    "disable_odoo_online",
    "crm",
    "contacts",
    "website_crm",
    "website_livechat",
    "crm_livechat",
    "base_automation",
    "auth_passkey",
    "tk_website",
    "tk_listings",
]
ATTACHMENT = "launchplane-check"
ATTACHMENT_BYTES: bytes = b"invented bytes for the quiet check\n"
ATTACHMENT_SHA256 = "5b8e2378c01c9392fc415eb0d02888d76f0a04ea8dec78a3860b07181260ffb4"
TK = Product(
    health_path="/web/login",
    database=Database(image="postgres:18-alpine"),
    odoo=Odoo(
        repository=REPOSITORY,
        workflow=WORKFLOW,
        image=IMAGE,
        addons_path=ADDONS_PATH,
        modules=MODULES,
        attachment=ATTACHMENT,
        sha256=ATTACHMENT_SHA256,
    ),
)
# The same product once its releases update a module on a copy of the lane's data.
TK_UPDATING = TK.model_copy(update={"odoo": TK.odoo and TK.odoo.model_copy(update={"update": ["tk_website"]})})
# The same product once an empty data set of it is initialized before its first web start. The name is invented.
TEST_LOGIN = "lp-tester"
TK_STARTING = TK.model_copy(
    update={
        "odoo": TK.odoo
        and TK.odoo.model_copy(
            update={"first_start": FirstStart(attachment=base64.b64encode(ATTACHMENT_BYTES), test_login=TEST_LOGIN)}
        )
    }
)
