from django.contrib.auth import get_user_model
from django.db import transaction

from faker import Faker

from baserow.contrib.builder.data_sources.handler import DataSourceHandler
from baserow.contrib.builder.domains.models import Domain
from baserow.contrib.builder.elements.handler import ElementHandler
from baserow.contrib.builder.elements.registries import element_type_registry
from baserow.contrib.builder.models import Builder
from baserow.contrib.builder.pages.handler import PageHandler
from baserow.contrib.builder.pages.models import Page
from baserow.contrib.database.table.models import Table
from baserow.contrib.database.views.models import GridView
from baserow.core.handler import CoreHandler
from baserow.core.integrations.handler import IntegrationHandler
from baserow.core.integrations.models import Integration
from baserow.core.integrations.registries import integration_type_registry
from baserow.core.services.registries import service_type_registry

User = get_user_model()


@transaction.atomic
def load_test_data():
    fake = Faker()
    print("Add builder basic data...")

    user = User.objects.get(email="admin@baserow.io")
    workspace = user.workspaceuser_set.get(workspace__name="Acme Corp").workspace

    try:
        builder = Builder.objects.get(
            name="Back to local website", workspace__isnull=False, trashed=False
        )
    except Builder.DoesNotExist:
        builder = CoreHandler().create_application(
            user, workspace, "builder", name="Back to local website"
        )

    Domain.objects.filter(domain_name="test1.getbaserow.io").delete()
    Domain.objects.filter(domain_name="test2.getbaserow.io").delete()
    Domain.objects.filter(domain_name="test3.getbaserow.io").delete()
    Domain.objects.create(builder=builder, domain_name="test1.getbaserow.io", order=1)
    Domain.objects.create(builder=builder, domain_name="test2.getbaserow.io", order=2)
    Domain.objects.create(builder=builder, domain_name="test3.getbaserow.io", order=3)

    integration_type = integration_type_registry.get("local_baserow")

    try:
        integration = Integration.objects.get(
            name="Local baserow", application__trashed=False, application_id=builder.id
        )
    except Integration.DoesNotExist:
        integration = IntegrationHandler().create_integration(
            integration_type, builder, name="Local baserow", authorized_user=user
        )

    heading_element_type = element_type_registry.get("heading")
    paragraph_element_type = element_type_registry.get("paragraph")
    link_element_type = element_type_registry.get("link")

    try:
        homepage = Page.objects.get(name="Homepage", builder=builder)
    except Page.DoesNotExist:
        homepage = PageHandler().create_page(builder, "Homepage", "/")

        ElementHandler().create_element(
            heading_element_type, homepage, value='"Back to local"', level=1
        )
        ElementHandler().create_element(
            heading_element_type, homepage, value='"Buy closer, Buy better"', level=2
        )
        content = "\n".join(fake.paragraphs(nb=2))
        ElementHandler().create_element(
            paragraph_element_type,
            homepage,
            value=f'"{content}"',
        )
        ElementHandler().create_element(
            heading_element_type,
            homepage,
            value='"Give more sense to what you eat"',
            level=2,
        )
        content = "\n".join(fake.paragraphs(nb=2))
        ElementHandler().create_element(
            paragraph_element_type,
            homepage,
            value=f'"{content}"',
        )

    try:
        terms = Page.objects.get(name="Terms", builder=builder)
    except Page.DoesNotExist:
        terms = PageHandler().create_page(builder, "Terms", "/terms")

        ElementHandler().create_element(
            heading_element_type, terms, value='"Terms"', level=1
        )
        ElementHandler().create_element(
            heading_element_type, terms, value='"Article 1. General"', level=2
        )
        content = "\n".join(fake.paragraphs(nb=3))
        ElementHandler().create_element(
            paragraph_element_type,
            terms,
            value=f'"{content}"',
        )
        ElementHandler().create_element(
            heading_element_type,
            terms,
            value='"Article 2. Services"',
            level=2,
        )
        content = "\n".join(fake.paragraphs(nb=3))
        ElementHandler().create_element(
            paragraph_element_type,
            terms,
            value=(f'"{content}"'),
        )
        ElementHandler().create_element(
            heading_element_type,
            terms,
            value='"Article 3. Data"',
            level=2,
        )
        content = "\n".join(fake.paragraphs(nb=3))
        ElementHandler().create_element(
            paragraph_element_type,
            terms,
            value=(f'"{content}"'),
        )

        ElementHandler().create_element(
            link_element_type,
            terms,
            value='"Home"',
            variant="button",
            alignment="right",
            navigation_type="page",
            navigate_to_page=homepage,
        )

        # Button for homepage
        ElementHandler().create_element(
            link_element_type,
            homepage,
            value='"See terms"',
            variant="button",
            alignment="right",
            navigation_type="page",
            navigate_to_page=terms,
        )

        ElementHandler().create_element(
            link_element_type,
            homepage,
            value='"Visit Baserow"',
            variant="link",
            alignment="center",
            navigation_type="custom",
            target="blank",
            navigate_to_url='"https://baserow.io"',
        )

    try:
        product_detail = Page.objects.get(name="Product detail", builder=builder)
    except Page.DoesNotExist:
        product_detail = PageHandler().create_page(
            builder,
            "Product detail",
            "/product/:id/:name",
            path_params=[
                {"name": "id", "type": "numeric"},
                {"name": "name", "type": "text"},
            ],
        )

        # Data source creation
        service_type = service_type_registry.get("local_baserow_get_row")
        table = Table.objects.get(
            name="Products",
            database__workspace=workspace,
            database__trashed=False,
        )
        view = GridView.objects.create(table=table, order=0, name="Products Grid")

        product_detail_data_source = DataSourceHandler().create_data_source(
            product_detail,
            "Product",
            service_type=service_type,
            view=view,
            integration=integration,
            row_id='get("page_parameter.id")',
        )

        ElementHandler().create_element(
            heading_element_type,
            product_detail,
            value=f'get("data_source.{product_detail_data_source.id}.Name")',
            level=1,
        )

        ElementHandler().create_element(
            paragraph_element_type,
            product_detail,
            value=(f'get("data_source.{product_detail_data_source.id}.Notes")'),
        )

    try:
        products = Page.objects.get(name="Products", builder=builder)
    except Page.DoesNotExist:
        products = PageHandler().create_page(builder, "Products", "/products")

        # Data source creation
        service_type = service_type_registry.get("local_baserow_list_rows")
        table = Table.objects.get(
            name="Products",
            database__workspace=workspace,
            database__trashed=False,
        )
        view = GridView.objects.create(table=table, order=0, name="Products Grid 2")

        products_data_source = DataSourceHandler().create_data_source(
            products,
            "Products",
            service_type=service_type,
            view=view,
            integration=integration,
        )

        ElementHandler().create_element(
            heading_element_type, products, value='"All products"', level=1
        )

        for i in range(3):
            ElementHandler().create_element(
                link_element_type,
                products,
                value=f'concat("Product {i+1} - ", get("data_source.{products_data_source.id}.{i}.Name"))',
                variant="button",
                alignment="left",
                navigation_type="page",
                navigate_to_page=product_detail,
                page_parameters=[
                    {"name": "id", "value": f"{i+1}"},
                    {
                        "name": "name",
                        "value": f'get("data_source.{products_data_source.id}.{i}.Name")',
                    },
                ],
            )

        ElementHandler().create_element(
            link_element_type,
            products,
            value=f'concat("Product 4 - ", get("data_source.{products_data_source.id}.3.Name"))',
            variant="button",
            alignment="left",
            navigation_type="custom",
            navigate_to_url=f'concat("/product/4/", get("data_source.{products_data_source.id}.3.Name"))',
        )

        ElementHandler().create_element(
            link_element_type,
            products,
            value='"Home"',
            variant="button",
            alignment="right",
            navigation_type="page",
            navigate_to_page=homepage,
        )

        # Button back from detail page
        ElementHandler().create_element(
            link_element_type,
            product_detail,
            value='"Back to list"',
            variant="button",
            alignment="left",
            navigation_type="page",
            navigate_to_page=products,
        )

        # Button back from detail page
        ElementHandler().create_element(
            link_element_type,
            homepage,
            value='"See product list"',
            variant="button",
            alignment="left",
            navigation_type="page",
            navigate_to_page=products,
        )
