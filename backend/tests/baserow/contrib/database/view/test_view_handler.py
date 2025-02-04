from decimal import Decimal
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import override_settings

import pytest

from baserow.contrib.database.fields.exceptions import FieldNotInTable
from baserow.contrib.database.fields.handler import FieldHandler
from baserow.contrib.database.fields.models import Field
from baserow.contrib.database.rows.handler import RowHandler
from baserow.contrib.database.search.handler import ALL_SEARCH_MODES, SearchHandler
from baserow.contrib.database.table.handler import TableHandler
from baserow.contrib.database.views.exceptions import (
    CannotShareViewTypeError,
    FormViewFieldTypeIsNotSupported,
    GridViewAggregationDoesNotSupportField,
    UnrelatedFieldError,
    ViewDoesNotExist,
    ViewDoesNotSupportFieldOptions,
    ViewFilterDoesNotExist,
    ViewFilterNotSupported,
    ViewFilterTypeDoesNotExist,
    ViewFilterTypeNotAllowedForField,
    ViewNotInTable,
    ViewOwnershipTypeDoesNotExist,
    ViewSortDoesNotExist,
    ViewSortFieldAlreadyExist,
    ViewSortFieldNotSupported,
    ViewSortNotSupported,
    ViewTypeDoesNotExist,
)
from baserow.contrib.database.views.handler import (
    PublicViewRows,
    ViewHandler,
    ViewIndexingHandler,
)
from baserow.contrib.database.views.models import (
    OWNERSHIP_TYPE_COLLABORATIVE,
    FormView,
    GridView,
    View,
    ViewFilter,
    ViewSort,
)
from baserow.contrib.database.views.registries import (
    view_aggregation_type_registry,
    view_filter_type_registry,
    view_type_registry,
)
from baserow.contrib.database.views.signals import view_loaded
from baserow.contrib.database.views.view_ownership_types import (
    CollaborativeViewOwnershipType,
)
from baserow.contrib.database.views.view_types import GridViewType
from baserow.core.exceptions import PermissionDenied, UserNotInWorkspace
from baserow.core.trash.handler import TrashHandler


@pytest.fixture(autouse=True)
def clean_registry_cache():
    """
    Ensure no patched version stays in cache.
    """

    view_type_registry.get_for_class.cache_clear()
    yield


@pytest.mark.django_db
def test_get_view(data_fixture):
    user = data_fixture.create_user()
    data_fixture.create_user()
    grid = data_fixture.create_grid_view(user=user)

    handler = ViewHandler()

    with pytest.raises(ViewDoesNotExist):
        handler.get_view_as_user(user, view_id=99999)

    view = handler.get_view_as_user(user, view_id=grid.id)

    assert view.id == grid.id
    assert view.name == grid.name
    assert view.filter_type == "AND"
    assert not view.filters_disabled
    assert isinstance(view, View)

    view = handler.get_view_as_user(user, view_id=grid.id, view_model=GridView)

    assert view.id == grid.id
    assert view.name == grid.name
    assert view.filter_type == "AND"
    assert not view.filters_disabled
    assert isinstance(view, GridView)

    # If the error is raised we know for sure that the query has resolved.
    with pytest.raises(AttributeError):
        handler.get_view_as_user(
            user,
            view_id=grid.id,
            base_queryset=View.objects.prefetch_related("UNKNOWN"),
        )

    # If the table is trashed the view should not be available.
    TrashHandler.trash(
        user, grid.table.database.workspace, grid.table.database, grid.table
    )
    with pytest.raises(ViewDoesNotExist):
        handler.get_view_as_user(user, view_id=grid.id, view_model=GridView)

    # Restoring the table should restore the view
    TrashHandler.restore_item(user, "table", grid.table.id)
    view = handler.get_view_as_user(user, view_id=grid.id, view_model=GridView)
    assert view.id == grid.id


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_created.send")
def test_create_grid_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    table_2 = data_fixture.create_database_table(user=user)

    handler = ViewHandler()
    view = handler.create_view(
        user=user, table=table, type_name="grid", name="Test grid"
    )

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == view.id
    assert send_mock.call_args[1]["user"].id == user.id

    assert View.objects.all().count() == 1
    assert GridView.objects.all().count() == 1

    grid = GridView.objects.all().first()
    assert grid.name == "Test grid"
    assert grid.order == 1
    assert grid.table == table
    assert grid.created_by == user
    assert grid.filter_type == "AND"
    assert not grid.filters_disabled

    handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Something else",
        filter_type="OR",
        filters_disabled=True,
    )

    assert View.objects.all().count() == 2
    assert GridView.objects.all().count() == 2

    grid = GridView.objects.all().last()
    assert grid.name == "Something else"
    assert grid.order == 2
    assert grid.table == table
    assert grid.filter_type == "OR"
    assert grid.filters_disabled

    grid = handler.create_view(
        user=user,
        table=table_2,
        type_name="grid",
        name="Name",
        filter_type="OR",
        filters_disabled=False,
    )

    assert View.objects.all().count() == 3
    assert GridView.objects.all().count() == 3

    assert grid.name == "Name"
    assert grid.order == 1
    assert grid.table == table_2
    assert grid.filter_type == "OR"
    assert not grid.filters_disabled

    with pytest.raises(UserNotInWorkspace):
        handler.create_view(user=user_2, table=table, type_name="grid", name="")

    with pytest.raises(ViewTypeDoesNotExist):
        handler.create_view(user=user, table=table, type_name="UNKNOWN", name="")


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_updated.send")
def test_update_grid_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid = data_fixture.create_grid_view(table=table)

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.update_view(user=user_2, view=grid, name="Test 1")

    with pytest.raises(ValueError):
        handler.update_view(user=user, view=object(), name="Test 1")

    view_with_changes = handler.update_view(user=user, view=grid, name="Test 1")
    view = view_with_changes.updated_view_instance

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == view.id
    assert send_mock.call_args[1]["user"].id == user.id

    grid.refresh_from_db()
    assert grid.name == "Test 1"
    assert grid.filter_type == "AND"
    assert not grid.filters_disabled

    handler.update_view(user=user, view=grid, filter_type="OR", filters_disabled=True)

    grid.refresh_from_db()
    assert grid.filter_type == "OR"
    assert grid.filters_disabled


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_deleted.send")
def test_delete_grid_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid = data_fixture.create_grid_view(table=table)

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.delete_view(user=user_2, view=grid)

    with pytest.raises(ValueError):
        handler.delete_view(user=user_2, view=object())

    view_id = grid.id

    assert View.objects.all().count() == 1
    handler.delete_view(user=user, view=grid)
    assert View.objects.all().count() == 0

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_id"] == view_id
    assert send_mock.call_args[1]["view"].id == view_id
    assert send_mock.call_args[1]["user"].id == user.id


@pytest.mark.django_db
def test_trashed_fields_are_not_included_in_grid_view_field_options(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid_view = data_fixture.create_grid_view(table=table)
    field_1 = data_fixture.create_text_field(table=table)
    field_2 = data_fixture.create_text_field(table=table)

    ViewHandler().update_field_options(
        user=user,
        view=grid_view,
        field_options={str(field_1.id): {"width": 150}, field_2.id: {"width": 250}},
    )
    options = grid_view.get_field_options()
    assert options.count() == 2

    TrashHandler.trash(user, table.database.workspace, table.database, field_1)

    options = grid_view.get_field_options()
    assert options.count() == 1

    with pytest.raises(UnrelatedFieldError):
        ViewHandler().update_field_options(
            user=user,
            view=grid_view,
            field_options={
                field_1.id: {"width": 150},
            },
        )


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_created.send")
def test_create_form_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_file_1 = data_fixture.create_user_file()
    user_file_2 = data_fixture.create_user_file()
    table = data_fixture.create_database_table(user=user)

    handler = ViewHandler()
    view = handler.create_view(user=user, table=table, type_name="form", name="Form")

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == view.id
    assert send_mock.call_args[1]["user"].id == user.id

    assert View.objects.all().count() == 1
    assert FormView.objects.all().count() == 1

    form = FormView.objects.all().first()
    assert len(str(form.slug)) == 43
    assert form.name == "Form"
    assert form.order == 1
    assert form.table == table
    assert form.title == ""
    assert form.description == ""
    assert form.cover_image is None
    assert form.logo_image is None
    assert form.submit_action == "MESSAGE"
    assert form.submit_action_redirect_url == ""

    form = handler.create_view(
        user=user,
        table=table,
        type_name="form",
        slug="test-slug",
        name="Form 2",
        public=True,
        title="Test form",
        description="Test form description",
        cover_image=user_file_1,
        logo_image=user_file_2,
        submit_action="REDIRECT",
        submit_action_redirect_url="https://localhost",
    )

    assert View.objects.all().count() == 2
    assert FormView.objects.all().count() == 2
    assert form.slug != "test-slug"
    assert len(form.slug) == 43
    assert form.name == "Form 2"
    assert form.order == 2
    assert form.table == table
    assert form.public is True
    assert form.title == "Test form"
    assert form.description == "Test form description"
    assert form.cover_image_id == user_file_1.id
    assert form.logo_image_id == user_file_2.id
    assert form.submit_action == "REDIRECT"
    assert form.submit_action_redirect_url == "https://localhost"


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_updated.send")
def test_update_form_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    user_file_1 = data_fixture.create_user_file()
    user_file_2 = data_fixture.create_user_file()

    handler = ViewHandler()
    view_with_changes = handler.update_view(
        user=user,
        view=form,
        slug="Test slug",
        name="Form 2",
        public=True,
        title="Test form",
        description="Test form description",
        cover_image=user_file_1,
        logo_image=user_file_2,
        submit_action="REDIRECT",
        submit_action_redirect_url="https://localhost",
    )
    view = view_with_changes.updated_view_instance

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == view.id
    assert send_mock.call_args[1]["user"].id == user.id

    form.refresh_from_db()
    assert form.slug != "test-slug"
    assert len(str(form.slug)) == 43
    assert form.name == "Form 2"
    assert form.table == table
    assert form.public is True
    assert form.title == "Test form"
    assert form.description == "Test form description"
    assert form.cover_image_id == user_file_1.id
    assert form.logo_image_id == user_file_2.id
    assert form.submit_action == "REDIRECT"
    assert form.submit_action_redirect_url == "https://localhost"


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_deleted.send")
def test_delete_form_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    data_fixture.create_text_field(table=table)
    form = data_fixture.create_form_view(table=table)

    handler = ViewHandler()
    view_id = form.id

    assert View.objects.all().count() == 1
    handler.delete_view(user=user, view=form)
    assert View.objects.all().count() == 0

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_id"] == view_id
    assert send_mock.call_args[1]["view"].id == view_id
    assert send_mock.call_args[1]["user"].id == user.id


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_created.send")
@patch("baserow.contrib.database.views.signals.views_reordered.send")
def test_duplicate_views(reordered_mock, created_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    field = data_fixture.create_text_field(table=table)
    grid = data_fixture.create_public_password_protected_grid_view(table=table, order=1)
    # Add another view to challenge the insertion position of the duplicate
    form = data_fixture.create_form_view(table=table, order=2)

    field_option = data_fixture.create_grid_view_field_option(
        grid_view=grid,
        field=field,
        aggregation_type="whatever",
        aggregation_raw_type="empty",
    )
    view_filter = data_fixture.create_view_filter(
        view=grid, field=field, value="test", type="equal"
    )
    view_sort = data_fixture.create_view_sort(view=grid, field=field, order="ASC")

    view_decoration = data_fixture.create_view_decoration(
        view=grid,
        value_provider_conf={"config": 12},
    )

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.duplicate_view(user=user_2, original_view=grid)

    new_view = handler.duplicate_view(user=user, original_view=grid)

    created_mock.assert_called_once()
    assert created_mock.call_args[1]["view"].id == new_view.id
    assert created_mock.call_args[1]["user"].id == user.id

    reordered_mock.assert_called_once()
    assert reordered_mock.call_args[1]["order"] == [grid.id, new_view.id, form.id]

    grid.refresh_from_db()
    assert new_view.name == grid.name + " 2"
    assert new_view.id != grid.id
    assert new_view.order == grid.order + 1
    assert new_view.public is False
    assert new_view.viewfilter_set.all().first().value == view_filter.value
    assert new_view.viewsort_set.all().first().order == view_sort.order
    assert (
        new_view.viewdecoration_set.all()[0].value_provider_conf
        == view_decoration.value_provider_conf
    )

    new_view2 = handler.duplicate_view(user=user, original_view=new_view)

    assert new_view2.name == new_view.name + " 2"

    new_view3 = handler.duplicate_view(user=user, original_view=grid)

    assert new_view3.name == grid.name + " 3"


@pytest.mark.django_db
def test_duplicate_views_with_multiple_select_has_filter(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    field = data_fixture.create_text_field(table=table)
    grid = data_fixture.create_public_password_protected_grid_view(table=table, order=1)
    data_fixture.create_view_filter(
        view=grid,
        field=field,
        type="multiple_select_has",
        value="1",
    )

    handler = ViewHandler()
    new_view = handler.duplicate_view(user=user, original_view=grid)
    new_filters = new_view.viewfilter_set.all()
    assert len(new_filters) == 1
    assert new_filters[0].view_id == new_view.id
    assert new_filters[0].field_id == field.id
    assert new_filters[0].type == "multiple_select_has"
    assert new_filters[0].value == "1"


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.views_reordered.send")
def test_order_views(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid_1 = data_fixture.create_grid_view(table=table, order=1)
    grid_2 = data_fixture.create_grid_view(table=table, order=2)
    grid_3 = data_fixture.create_grid_view(table=table, order=3)
    grid_diff_ownership = data_fixture.create_grid_view(table=table, order=2)
    grid_diff_ownership.ownership_type = "personal"
    grid_diff_ownership.save()
    grid_diff_ownership2 = data_fixture.create_grid_view(table=table, order=3)
    grid_diff_ownership2.ownership_type = "personal"
    grid_diff_ownership2.save()

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.order_views(user=user_2, table=table, order=[])

    with pytest.raises(ViewNotInTable):
        handler.order_views(user=user, table=table, order=[0])

    with pytest.raises(ViewNotInTable):
        handler.order_views(
            user=user,
            table=table,
            order=[grid_diff_ownership.id, grid_3.id, grid_2.id, grid_1.id],
        )

    with pytest.raises(ViewNotInTable):
        handler.order_views(
            user=user,
            table=table,
            order=[grid_diff_ownership.id, grid_diff_ownership2.id],
        )

    handler.order_views(
        user=user,
        table=table,
        order=[grid_3.id, grid_2.id, grid_1.id],
    )
    grid_1.refresh_from_db()
    grid_2.refresh_from_db()
    grid_3.refresh_from_db()
    assert grid_1.order == 3
    assert grid_2.order == 2
    assert grid_3.order == 1

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["table"].id == table.id
    assert send_mock.call_args[1]["user"].id == user.id
    assert send_mock.call_args[1]["order"] == [grid_3.id, grid_2.id, grid_1.id]

    handler.order_views(
        user=user,
        table=table,
        order=[grid_1.id, grid_3.id, grid_2.id],
    )
    grid_1.refresh_from_db()
    grid_2.refresh_from_db()
    grid_3.refresh_from_db()
    assert grid_1.order == 1
    assert grid_2.order == 3
    assert grid_3.order == 2

    handler.order_views(user=user, table=table, order=[grid_1.id])
    grid_1.refresh_from_db()
    grid_2.refresh_from_db()
    grid_3.refresh_from_db()
    assert grid_1.order == 1
    assert grid_2.order == 3
    assert grid_3.order == 2


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_deleted.send")
def test_delete_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid = data_fixture.create_grid_view(table=table)

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.delete_view(user=user_2, view=grid)

    with pytest.raises(ValueError):
        handler.delete_view(user=user_2, view=object())

    view_id = grid.id

    assert View.objects.all().count() == 1
    handler.delete_view(user=user, view=grid)
    assert View.objects.all().count() == 0

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_id"] == view_id
    assert send_mock.call_args[1]["view"].id == view_id
    assert send_mock.call_args[1]["user"].id == user.id


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_field_options_updated.send")
def test_update_field_options(send_mock, data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    grid_view = data_fixture.create_grid_view(table=table)
    field_1 = data_fixture.create_text_field(table=table)
    field_2 = data_fixture.create_text_field(table=table)
    field_3 = data_fixture.create_text_field()

    with pytest.raises(ValueError):
        ViewHandler().update_field_options(
            user=user,
            view=grid_view,
            field_options={
                "strange_format": {"height": 150},
            },
        )

    with pytest.raises(UserNotInWorkspace):
        ViewHandler().update_field_options(
            user=data_fixture.create_user(),
            view=grid_view,
            field_options={
                "strange_format": {"height": 150},
            },
        )

    with pytest.raises(UnrelatedFieldError):
        ViewHandler().update_field_options(
            user=user,
            view=grid_view,
            field_options={
                99999: {"width": 150},
            },
        )

    with pytest.raises(UnrelatedFieldError):
        ViewHandler().update_field_options(
            user=user,
            view=grid_view,
            field_options={
                field_3.id: {"width": 150},
            },
        )

    with pytest.raises(ViewDoesNotSupportFieldOptions):
        ViewHandler().update_field_options(
            user=user,
            # The View object does not have the `field_options` field, so we expect
            # it to fail.
            view=View.objects.get(pk=grid_view.id),
            field_options={
                field_1.id: {"width": 150},
            },
        )

    ViewHandler().update_field_options(
        user=user,
        view=grid_view,
        field_options={str(field_1.id): {"width": 150}, field_2.id: {"width": 250}},
    )
    options_4 = grid_view.get_field_options()

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == grid_view.id
    assert send_mock.call_args[1]["user"].id == user.id
    assert len(options_4) == 2
    assert options_4[0].width == 150
    assert options_4[0].field_id == field_1.id
    assert options_4[1].width == 250
    assert options_4[1].field_id == field_2.id

    field_4 = data_fixture.create_text_field(table=table)
    ViewHandler().update_field_options(
        user=user,
        view=grid_view,
        field_options={field_2.id: {"width": 300}, field_4.id: {"width": 50}},
    )
    options_4 = grid_view.get_field_options()
    assert len(options_4) == 3
    assert options_4[0].width == 150
    assert options_4[0].field_id == field_1.id
    assert options_4[1].width == 300
    assert options_4[1].field_id == field_2.id
    assert options_4[2].width == 50
    assert options_4[2].field_id == field_4.id


@pytest.mark.django_db
def test_grid_view_aggregation_type_field_option(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    field_1 = data_fixture.create_text_field(table=table)
    grid_view = data_fixture.create_grid_view(table=table)

    # Fake incompatible field
    empty_count = view_aggregation_type_registry.get("empty_count")
    empty_count.field_is_compatible = lambda _: False

    with pytest.raises(GridViewAggregationDoesNotSupportField):
        ViewHandler().update_field_options(
            user=user,
            view=grid_view,
            field_options={
                field_1.id: {"aggregation_raw_type": "empty_count"},
            },
        )

    empty_count.field_is_compatible = lambda _: True


@pytest.mark.django_db
def test_enable_form_view_file_field(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form_view = data_fixture.create_form_view(table=table)
    created_on_field = data_fixture.create_created_on_field(table=table)

    with pytest.raises(FormViewFieldTypeIsNotSupported):
        ViewHandler().update_field_options(
            user=user,
            view=form_view,
            field_options={
                created_on_field.id: {"enabled": True},
            },
        )

    ViewHandler().update_field_options(
        user=user,
        view=form_view,
        field_options={
            created_on_field.id: {"enabled": False},
        },
    )


@pytest.mark.django_db
def test_field_type_changed(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    table_2 = data_fixture.create_database_table(user=user, database=table.database)
    text_field = data_fixture.create_text_field(table=table)
    grid_view = data_fixture.create_grid_view(table=table)
    data_fixture.create_view_filter(
        view=grid_view, field=text_field, type="contains", value="test"
    )
    data_fixture.create_view_sort(view=grid_view, field=text_field, order="ASC")

    field_handler = FieldHandler()
    long_text_field = field_handler.update_field(
        user=user, field=text_field, new_type_name="long_text"
    )
    assert ViewFilter.objects.all().count() == 1
    assert ViewSort.objects.all().count() == 1

    field_handler.update_field(
        user=user, field=long_text_field, new_type_name="boolean"
    )
    assert ViewFilter.objects.all().count() == 0
    assert ViewSort.objects.all().count() == 1

    field_handler.update_field(
        user=user,
        field=long_text_field,
        new_type_name="link_row",
        link_row_table=table_2,
    )
    assert ViewFilter.objects.all().count() == 0
    assert ViewSort.objects.all().count() == 0


@pytest.mark.django_db
def test_apply_filters(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(table=table)
    number_field = data_fixture.create_number_field(table=table)
    boolean_field = data_fixture.create_boolean_field(table=table)
    grid_view = data_fixture.create_grid_view(table=table)

    view_handler = ViewHandler()

    model = table.get_model()
    row_1 = model.objects.create(
        **{
            f"field_{text_field.id}": "Value 1",
            f"field_{number_field.id}": 10,
            f"field_{boolean_field.id}": True,
        }
    )
    row_2 = model.objects.create(
        **{
            f"field_{text_field.id}": "Entry 2",
            f"field_{number_field.id}": 20,
            f"field_{boolean_field.id}": False,
        }
    )
    row_3 = model.objects.create(
        **{
            f"field_{text_field.id}": "Item 3",
            f"field_{number_field.id}": 30,
            f"field_{boolean_field.id}": True,
        }
    )
    row_4 = model.objects.create(
        **{
            f"field_{text_field.id}": "",
            f"field_{number_field.id}": None,
            f"field_{boolean_field.id}": False,
        }
    )

    filter_1 = data_fixture.create_view_filter(
        view=grid_view, field=text_field, type="equal", value="Value 1"
    )

    # Should raise a value error if the model doesn't have the _field_objects property.
    with pytest.raises(ValueError):
        view_handler.apply_filters(grid_view, GridView.objects.all())

    # Should raise a value error if the field is not included in the model.
    with pytest.raises(ValueError):
        view_handler.apply_filters(
            grid_view, table.get_model(field_ids=[]).objects.all()
        )

    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 1
    assert rows[0].id == row_1.id

    filter_2 = data_fixture.create_view_filter(
        view=grid_view, field=number_field, type="equal", value="20"
    )
    filter_1.value = "Entry 2"
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 1
    assert rows[0].id == row_2.id

    filter_1.value = "Item 3"
    filter_1.type = "equal"
    filter_1.save()
    filter_2.value = "20"
    filter_2.type = "not_equal"
    filter_2.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 1
    assert rows[0].id == row_3.id

    grid_view.filter_type = "OR"
    filter_1.value = "Value 1"
    filter_1.type = "equal"
    filter_1.save()
    filter_2.field = text_field
    filter_2.value = "Entry 2"
    filter_2.type = "equal"
    filter_2.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 2
    rows_0, rows_1 = rows
    assert rows_0.id == row_1.id
    assert rows_1.id == row_2.id

    filter_2.delete()

    grid_view.filter_type = "AND"
    filter_1.value = ""
    filter_1.type = "empty"
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 1
    assert rows[0].id == row_4.id

    grid_view.filter_type = "AND"
    filter_1.value = ""
    filter_1.type = "not_empty"
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 3
    rows_0, rows_1, rows_2 = rows
    assert rows_0.id == row_1.id
    assert rows_1.id == row_2.id
    assert rows_2.id == row_3.id

    grid_view.filter_type = "AND"
    filter_1.value = "1"
    filter_1.type = "equal"
    filter_1.field = boolean_field
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 2
    rows_0, rows_1 = rows
    assert rows_0.id == row_1.id
    assert rows_1.id == row_3.id

    grid_view.filter_type = "AND"
    filter_1.value = "1"
    filter_1.type = "not_equal"
    filter_1.field = boolean_field
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 2
    rows_0, rows_1 = rows
    assert rows_0.id == row_2.id
    assert rows_1.id == row_4.id

    grid_view.filter_type = "AND"
    filter_1.value = "False"
    filter_1.type = "equal"
    filter_1.field = boolean_field
    filter_1.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    assert len(rows) == 2
    rows_0, rows_1 = rows
    assert rows_0.id == row_2.id
    assert rows_1.id == row_4.id

    grid_view.filters_disabled = True
    grid_view.save()
    rows = view_handler.apply_filters(grid_view, model.objects.all())
    rows_0, rows_1, rows_2, rows_3 = rows
    assert rows_0.id == row_1.id
    assert rows_1.id == row_2.id
    assert rows_2.id == row_3.id
    assert rows_3.id == row_4.id


@pytest.mark.django_db
def test_get_filter(data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    equal_filter = data_fixture.create_view_filter(user=user)

    handler = ViewHandler()

    with pytest.raises(ViewFilterDoesNotExist):
        handler.get_filter(user=user, view_filter_id=99999)

    with pytest.raises(UserNotInWorkspace):
        handler.get_filter(user=user_2, view_filter_id=equal_filter.id)

    with pytest.raises(AttributeError):
        handler.get_filter(
            user=user,
            view_filter_id=equal_filter.id,
            base_queryset=ViewFilter.objects.prefetch_related("UNKNOWN"),
        )

    view_filter = handler.get_filter(user=user, view_filter_id=equal_filter.id)

    assert view_filter.id == equal_filter.id
    assert view_filter.view_id == equal_filter.view_id
    assert view_filter.field_id == equal_filter.field_id
    assert view_filter.type == equal_filter.type
    assert view_filter.value == equal_filter.value


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_filter_created.send")
def test_create_filter(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    text_field = data_fixture.create_text_field(table=grid_view.table)
    other_field = data_fixture.create_text_field()

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.create_filter(
            user=user_2,
            view=grid_view,
            field=text_field,
            type_name="equal",
            value="test",
        )

    grid_view_type = view_type_registry.get("grid")
    grid_view_type.can_filter = False
    with pytest.raises(ViewFilterNotSupported):
        handler.create_filter(
            user=user, view=grid_view, field=text_field, type_name="equal", value="test"
        )
    grid_view_type.can_filter = True

    with pytest.raises(ViewFilterTypeDoesNotExist):
        handler.create_filter(
            user=user,
            view=grid_view,
            field=text_field,
            type_name="NOT_EXISTS",
            value="test",
        )

    equal_filter_type = view_filter_type_registry.get("equal")
    allowed = equal_filter_type.compatible_field_types
    equal_filter_type.compatible_field_types = []
    with pytest.raises(ViewFilterTypeNotAllowedForField):
        handler.create_filter(
            user=user, view=grid_view, field=text_field, type_name="equal", value="test"
        )
    equal_filter_type.compatible_field_types = allowed

    with pytest.raises(FieldNotInTable):
        handler.create_filter(
            user=user,
            view=grid_view,
            field=other_field,
            type_name="equal",
            value="test",
        )

    view_filter = handler.create_filter(
        user=user, view=grid_view, field=text_field, type_name="equal", value="test"
    )

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_filter"].id == view_filter.id
    assert send_mock.call_args[1]["user"].id == user.id

    assert ViewFilter.objects.all().count() == 1
    first = ViewFilter.objects.all().first()

    assert view_filter.id == first.id
    assert view_filter.view_id == grid_view.id
    assert view_filter.field_id == text_field.id
    assert view_filter.type == "equal"
    assert view_filter.value == "test"

    tmp_field = Field.objects.get(pk=text_field.id)
    view_filter_2 = handler.create_filter(
        user=user, view=grid_view, field=tmp_field, type_name="equal", value="test"
    )
    assert view_filter_2.view_id == grid_view.id
    assert view_filter_2.field_id == text_field.id
    assert view_filter_2.type == "equal"
    assert view_filter_2.value == "test"


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_filter_updated.send")
def test_update_filter(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    text_field = data_fixture.create_text_field(table=grid_view.table)
    long_text_field = data_fixture.create_long_text_field(table=grid_view.table)
    other_field = data_fixture.create_text_field()
    equal_filter = data_fixture.create_view_filter(
        view=grid_view, field=long_text_field, type="equal", value="test1"
    )

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.update_filter(user=user_2, view_filter=equal_filter)

    with pytest.raises(ViewFilterTypeDoesNotExist):
        handler.update_filter(
            user=user, view_filter=equal_filter, type_name="NOT_EXISTS"
        )

    equal_filter_type = view_filter_type_registry.get("equal")
    allowed = equal_filter_type.compatible_field_types
    equal_filter_type.compatible_field_types = []
    with pytest.raises(ViewFilterTypeNotAllowedForField):
        handler.update_filter(user=user, view_filter=equal_filter, field=text_field)

    equal_filter_type.compatible_field_types = [lambda _: False]
    with pytest.raises(ViewFilterTypeNotAllowedForField):
        handler.update_filter(user=user, view_filter=equal_filter, field=text_field)

    equal_filter_type.compatible_field_types = allowed

    with pytest.raises(FieldNotInTable):
        handler.update_filter(user=user, view_filter=equal_filter, field=other_field)

    updated_filter = handler.update_filter(
        user=user, view_filter=equal_filter, value="test2"
    )
    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_filter"].id == updated_filter.id
    assert send_mock.call_args[1]["user"].id == user.id
    assert updated_filter.value == "test2"
    assert updated_filter.field_id == long_text_field.id
    assert updated_filter.type == "equal"
    assert updated_filter.view_id == grid_view.id

    updated_filter = handler.update_filter(
        user=user,
        view_filter=equal_filter,
        value="test3",
        field=text_field,
        type_name="not_equal",
    )
    assert updated_filter.value == "test3"
    assert updated_filter.field_id == text_field.id
    assert updated_filter.type == "not_equal"
    assert updated_filter.view_id == grid_view.id


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_filter_deleted.send")
def test_delete_filter(send_mock, data_fixture):
    user = data_fixture.create_user()
    filter_1 = data_fixture.create_view_filter(user=user)
    filter_2 = data_fixture.create_view_filter()

    assert ViewFilter.objects.all().count() == 2

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.delete_filter(user=user, view_filter=filter_2)

    filter_1_id = filter_1.id
    handler.delete_filter(user=user, view_filter=filter_1)

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_filter_id"] == filter_1_id
    assert send_mock.call_args[1]["view_filter"]
    assert send_mock.call_args[1]["user"].id == user.id
    assert ViewFilter.objects.all().count() == 1
    assert ViewFilter.objects.filter(pk=filter_1.pk).count() == 0


@pytest.mark.django_db
def test_apply_sortings(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(table=table)
    number_field = data_fixture.create_number_field(table=table)
    boolean_field = data_fixture.create_boolean_field(table=table)
    grid_view = data_fixture.create_grid_view(table=table)

    view_handler = ViewHandler()

    model = table.get_model()
    row_1 = model.objects.create(
        **{
            f"field_{text_field.id}": "Aaa",
            f"field_{number_field.id}": 30,
            f"field_{boolean_field.id}": True,
        }
    )
    row_2 = model.objects.create(
        **{
            f"field_{text_field.id}": "Aaa",
            f"field_{number_field.id}": 20,
            f"field_{boolean_field.id}": True,
        }
    )
    row_3 = model.objects.create(
        **{
            f"field_{text_field.id}": "Aaa",
            f"field_{number_field.id}": 10,
            f"field_{boolean_field.id}": False,
        }
    )
    row_4 = model.objects.create(
        **{
            f"field_{text_field.id}": "Bbbb",
            f"field_{number_field.id}": 60,
            f"field_{boolean_field.id}": False,
        }
    )
    row_5 = model.objects.create(
        **{
            f"field_{text_field.id}": "Cccc",
            f"field_{number_field.id}": 50,
            f"field_{boolean_field.id}": False,
        }
    )
    row_6 = model.objects.create(
        **{
            f"field_{text_field.id}": "Dddd",
            f"field_{number_field.id}": 40,
            f"field_{boolean_field.id}": True,
        }
    )

    # Without any sortings.
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_1.id, row_2.id, row_3.id, row_4.id, row_5.id, row_6.id]

    sort = data_fixture.create_view_sort(view=grid_view, field=text_field, order="ASC")

    # Should raise a value error if the modal doesn't have the _field_objects property.
    with pytest.raises(ValueError):
        view_handler.apply_sorting(grid_view, GridView.objects.all())

    # Should raise a value error if the field is not included in the model.
    with pytest.raises(ValueError):
        view_handler.apply_sorting(
            grid_view, table.get_model(field_ids=[]).objects.all()
        )

    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_1.id, row_2.id, row_3.id, row_4.id, row_5.id, row_6.id]

    sort.order = "DESC"
    sort.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_6.id, row_5.id, row_4.id, row_1.id, row_2.id, row_3.id]

    sort.order = "ASC"
    sort.field_id = number_field.id
    sort.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_3.id, row_2.id, row_1.id, row_6.id, row_5.id, row_4.id]

    sort.field_id = boolean_field.id
    sort.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_3.id, row_4.id, row_5.id, row_1.id, row_2.id, row_6.id]

    sort.field_id = text_field.id
    sort.save()
    sort_2 = data_fixture.create_view_sort(
        view=grid_view, field=number_field, order="ASC"
    )
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_3.id, row_2.id, row_1.id, row_4.id, row_5.id, row_6.id]

    sort.field_id = text_field.id
    sort.save()
    sort_2.field_id = boolean_field
    sort_2.order = "DESC"
    sort_2.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_1.id, row_2.id, row_3.id, row_4.id, row_5.id, row_6.id]

    sort.field_id = text_field.id
    sort.order = "DESC"
    sort.save()
    sort_2.field_id = boolean_field
    sort_2.order = "ASC"
    sort_2.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_6.id, row_5.id, row_4.id, row_3.id, row_1.id, row_2.id]

    sort.field_id = number_field.id
    sort.save()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [row_4.id, row_5.id, row_6.id, row_1.id, row_2.id, row_3.id]

    row_7 = model.objects.create(
        **{
            f"field_{text_field.id}": "Aaa",
            f"field_{number_field.id}": 30,
            f"field_{boolean_field.id}": True,
            "order": Decimal("0.1"),
        }
    )

    sort.delete()
    sort_2.delete()
    rows = view_handler.apply_sorting(grid_view, model.objects.all())
    row_ids = [row.id for row in rows]
    assert row_ids == [
        row_7.id,
        row_1.id,
        row_2.id,
        row_3.id,
        row_4.id,
        row_5.id,
        row_6.id,
    ]


@pytest.mark.django_db
def test_get_sort(data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    equal_sort = data_fixture.create_view_sort(user=user)

    handler = ViewHandler()

    with pytest.raises(ViewSortDoesNotExist):
        handler.get_sort(user=user, view_sort_id=99999)

    with pytest.raises(UserNotInWorkspace):
        handler.get_sort(user=user_2, view_sort_id=equal_sort.id)

    with pytest.raises(AttributeError):
        handler.get_sort(
            user=user,
            view_sort_id=equal_sort.id,
            base_queryset=ViewSort.objects.prefetch_related("UNKNOWN"),
        )

    sort = handler.get_sort(user=user, view_sort_id=equal_sort.id)

    assert sort.id == equal_sort.id
    assert sort.view_id == equal_sort.view_id
    assert sort.field_id == equal_sort.field_id
    assert sort.order == equal_sort.order


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_sort_created.send")
def test_create_sort(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    text_field = data_fixture.create_text_field(table=grid_view.table)
    text_field_2 = data_fixture.create_text_field(table=grid_view.table)
    link_row_field = data_fixture.create_link_row_field(table=grid_view.table)
    other_field = data_fixture.create_text_field()

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.create_sort(user=user_2, view=grid_view, field=text_field, order="ASC")

    grid_view_type = view_type_registry.get("grid")
    grid_view_type.can_sort = False
    with pytest.raises(ViewSortNotSupported):
        handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    grid_view_type.can_sort = True

    with pytest.raises(ViewSortFieldNotSupported):
        handler.create_sort(
            user=user, view=grid_view, field=link_row_field, order="ASC"
        )

    with pytest.raises(FieldNotInTable):
        handler.create_sort(user=user, view=grid_view, field=other_field, order="ASC")

    view_sort = handler.create_sort(
        user=user, view=grid_view, field=text_field, order="ASC"
    )

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_sort"].id == view_sort.id
    assert send_mock.call_args[1]["user"].id == user.id

    assert ViewSort.objects.all().count() == 1
    first = ViewSort.objects.all().first()

    assert view_sort.id == first.id
    assert view_sort.view_id == grid_view.id
    assert view_sort.field_id == text_field.id
    assert view_sort.order == "ASC"

    with pytest.raises(ViewSortFieldAlreadyExist):
        handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")

    view_sort_2 = handler.create_sort(
        user=user, view=grid_view, field=text_field_2, order="DESC"
    )
    assert view_sort_2.view_id == grid_view.id
    assert view_sort_2.field_id == text_field_2.id
    assert view_sort_2.order == "DESC"
    assert ViewSort.objects.all().count() == 2


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_sort_updated.send")
def test_update_sort(send_mock, data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    text_field = data_fixture.create_text_field(table=grid_view.table)
    long_text_field = data_fixture.create_long_text_field(table=grid_view.table)
    link_row_field = data_fixture.create_link_row_field(table=grid_view.table)
    other_field = data_fixture.create_text_field()
    view_sort = data_fixture.create_view_sort(
        view=grid_view,
        field=long_text_field,
        order="ASC",
    )

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.update_sort(user=user_2, view_sort=view_sort)

    with pytest.raises(ViewSortFieldNotSupported):
        handler.update_sort(user=user, view_sort=view_sort, field=link_row_field)

    with pytest.raises(FieldNotInTable):
        handler.update_sort(user=user, view_sort=view_sort, field=other_field)

    updated_sort = handler.update_sort(user=user, view_sort=view_sort, order="DESC")
    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_sort"].id == updated_sort.id
    assert send_mock.call_args[1]["user"].id == user.id
    assert updated_sort.order == "DESC"
    assert updated_sort.field_id == long_text_field.id
    assert updated_sort.view_id == grid_view.id

    updated_sort = handler.update_sort(
        user=user, view_sort=updated_sort, order="ASC", field=text_field
    )
    assert updated_sort.order == "ASC"
    assert updated_sort.field_id == text_field.id
    assert updated_sort.view_id == grid_view.id

    data_fixture.create_view_sort(view=grid_view, field=long_text_field)

    with pytest.raises(ViewSortFieldAlreadyExist):
        handler.update_sort(
            user=user, view_sort=view_sort, order="ASC", field=long_text_field
        )


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_sort_deleted.send")
def test_delete_sort(send_mock, data_fixture):
    user = data_fixture.create_user()
    sort_1 = data_fixture.create_view_sort(user=user)
    sort_2 = data_fixture.create_view_sort()

    assert ViewSort.objects.all().count() == 2

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.delete_sort(user=user, view_sort=sort_2)

    sort_1_id = sort_1.id
    handler.delete_sort(user=user, view_sort=sort_1)

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view_sort_id"] == sort_1_id
    assert send_mock.call_args[1]["view_sort"]
    assert send_mock.call_args[1]["user"].id == user.id

    assert ViewSort.objects.all().count() == 1
    assert ViewSort.objects.filter(pk=sort_1.pk).count() == 0


@pytest.mark.django_db
@patch("baserow.contrib.database.views.signals.view_updated.send")
def test_rotate_view_slug(send_mock, data_fixture):
    class UnShareableViewType(GridViewType):
        can_share = False

    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    grid = data_fixture.create_grid_view(table=table)
    old_slug = str(form.slug)

    handler = ViewHandler()

    with pytest.raises(UserNotInWorkspace):
        handler.rotate_view_slug(user=user_2, view=form)

    with patch.dict(view_type_registry.registry, {"grid": UnShareableViewType()}):
        with pytest.raises(CannotShareViewTypeError):
            handler.rotate_view_slug(user=user, view=grid)

    handler.rotate_view_slug(user=user, view=form)

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["view"].id == form.id
    assert send_mock.call_args[1]["user"].id == user.id

    form.refresh_from_db()
    assert str(form.slug) != old_slug
    assert len(str(form.slug)) == 43


@pytest.mark.django_db
def test_get_public_view_by_slug(data_fixture):
    user = data_fixture.create_user()
    user_2 = data_fixture.create_user()
    form = data_fixture.create_form_view(user=user)

    handler = ViewHandler()

    with pytest.raises(ViewDoesNotExist):
        handler.get_public_view_by_slug(user_2, "not_existing")

    with pytest.raises(ViewDoesNotExist):
        handler.get_public_view_by_slug(user_2, "a3f1493a-9229-4889-8531-6a65e745602e")

    with pytest.raises(ViewDoesNotExist):
        handler.get_public_view_by_slug(user_2, form.slug)

    form2 = handler.get_public_view_by_slug(user, form.slug)
    assert form.id == form2.id

    form.public = True
    form.save()

    form2 = handler.get_public_view_by_slug(user_2, form.slug)
    assert form.id == form2.id

    form3 = handler.get_public_view_by_slug(user_2, form.slug, view_model=FormView)
    assert form.id == form3.id
    assert isinstance(form3, FormView)


@pytest.mark.django_db
@patch("baserow.contrib.database.rows.signals.rows_created.send")
def test_submit_form_view(send_mock, data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    text_field = data_fixture.create_text_field(table=table)
    number_field = data_fixture.create_number_field(table=table)
    boolean_field = data_fixture.create_boolean_field(table=table)
    data_fixture.create_form_view_field_option(
        form, text_field, required=True, enabled=True
    )
    data_fixture.create_form_view_field_option(
        form, number_field, required=False, enabled=True
    )
    data_fixture.create_form_view_field_option(
        form, boolean_field, required=True, enabled=False
    )

    handler = ViewHandler()

    with pytest.raises(ValidationError) as e:
        handler.submit_form_view(user, form=form, values={})

    with pytest.raises(ValidationError) as e:
        handler.submit_form_view(
            user, form=form, values={f"field_{number_field.id}": 0}
        )

    assert f"field_{text_field.id}" in e.value.error_dict

    instance = handler.submit_form_view(
        None, form=form, values={f"field_{text_field.id}": "Text value"}
    )

    send_mock.assert_called_once()
    assert send_mock.call_args[1]["rows"][0].id == instance.id
    assert send_mock.call_args[1]["user"] is None
    assert send_mock.call_args[1]["table"].id == table.id
    assert send_mock.call_args[1]["before"] is None
    assert send_mock.call_args[1]["model"]._generated_table_model

    handler.submit_form_view(
        user,
        form=form,
        values={
            f"field_{text_field.id}": "Another value",
            f"field_{number_field.id}": 10,
            f"field_{boolean_field.id}": True,
        },
    )

    model = table.get_model()
    all = model.objects.all()
    assert len(all) == 2
    assert getattr(all[0], f"field_{text_field.id}") == "Text value"
    assert getattr(all[0], f"field_{number_field.id}") is None
    assert not getattr(all[0], f"field_{boolean_field.id}")
    assert getattr(all[1], f"field_{text_field.id}") == "Another value"
    assert getattr(all[1], f"field_{number_field.id}") == 10
    assert not getattr(all[1], f"field_{boolean_field.id}")


@pytest.mark.django_db
def test_submit_form_view_skip_required_with_conditions(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    text_field = data_fixture.create_text_field(table=table)
    number_field = data_fixture.create_number_field(table=table)
    data_fixture.create_form_view_field_option(
        form, text_field, required=True, enabled=True
    )
    number_option = data_fixture.create_form_view_field_option(
        form, number_field, required=True, enabled=True
    )

    handler = ViewHandler()

    with pytest.raises(ValidationError):
        handler.submit_form_view(
            user, form=form, values={f"field_{text_field.id}": "1"}
        )

    number_option.show_when_matching_conditions = True
    number_option.save()

    with pytest.raises(ValidationError):
        handler.submit_form_view(
            user, form=form, values={f"field_{text_field.id}": "1"}
        )

    # When there is a condition and `show_when_matching_conditions` is `True`,
    # the backend can't validate whether the values match the filter, we we don't do
    # a required validation at all.
    data_fixture.create_form_view_field_options_condition(
        field_option=number_option, field=text_field
    )

    handler.submit_form_view(user, form=form, values={f"field_{text_field.id}": "1"})
    model = table.get_model()
    assert model.objects.all().count() == 1


@pytest.mark.django_db
def test_get_public_views_which_include_row(data_fixture, django_assert_num_queries):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    visible_field = data_fixture.create_text_field(table=table)
    hidden_field = data_fixture.create_text_field(table=table)
    public_view1 = data_fixture.create_grid_view(
        user,
        create_options=False,
        table=table,
        public=True,
        order=0,
    )
    public_view2 = data_fixture.create_grid_view(
        user, table=table, public=True, order=1
    )
    public_view3 = data_fixture.create_grid_view(
        user, table=table, public=True, order=2
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)
    data_fixture.create_grid_view(user, table=table)
    data_fixture.create_grid_view_field_option(public_view1, hidden_field, hidden=True)
    data_fixture.create_grid_view_field_option(public_view2, hidden_field, hidden=True)

    # Public View 1 has filters which match row 1
    data_fixture.create_view_filter(
        view=public_view1, field=visible_field, type="equal", value="Visible"
    )
    data_fixture.create_view_filter(
        view=public_view1, field=hidden_field, type="equal", value="Hidden"
    )

    # Public View 2 has filters which match row 2
    data_fixture.create_view_filter(
        view=public_view2, field=visible_field, type="equal", value="Visible"
    )
    data_fixture.create_view_filter(
        view=public_view2, field=hidden_field, type="equal", value="Not Match"
    )

    # Public View 3 has filters which match both rows
    data_fixture.create_view_filter(
        view=public_view2, field=visible_field, type="equal", value="Visible"
    )

    # Private View 1 has no filters so matches both rows

    row = RowHandler().create_row(
        user=user,
        table=table,
        values={
            f"field_{visible_field.id}": "Visible",
            f"field_{hidden_field.id}": "Hidden",
        },
    )
    row2 = RowHandler().create_row(
        user=user,
        table=table,
        values={
            f"field_{visible_field.id}": "Visible",
            f"field_{hidden_field.id}": "Not Match",
        },
    )

    model = table.get_model()
    checker = ViewHandler().get_public_views_row_checker(
        table, model, only_include_views_which_want_realtime_events=True
    )
    assert checker.get_public_views_where_row_is_visible(row) == [
        public_view1.view_ptr.specific,
        public_view3.view_ptr.specific,
    ]
    assert checker.get_public_views_where_row_is_visible(row2) == [
        public_view2.view_ptr.specific,
        public_view3.view_ptr.specific,
    ]


@pytest.mark.django_db
def test_get_public_views_which_include_rows(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    visible_field = data_fixture.create_text_field(table=table)
    hidden_field = data_fixture.create_text_field(table=table)
    public_view1 = data_fixture.create_grid_view(
        user,
        create_options=False,
        table=table,
        public=True,
        order=0,
    )
    public_view2 = data_fixture.create_grid_view(
        user, table=table, public=True, order=1
    )
    public_view3 = data_fixture.create_grid_view(
        user, table=table, public=True, order=2
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)
    data_fixture.create_grid_view(user, table=table)
    data_fixture.create_grid_view_field_option(public_view1, hidden_field, hidden=True)
    data_fixture.create_grid_view_field_option(public_view2, hidden_field, hidden=True)

    # Public View 1 has filters which match row 1
    data_fixture.create_view_filter(
        view=public_view1, field=visible_field, type="equal", value="Visible"
    )
    data_fixture.create_view_filter(
        view=public_view1, field=hidden_field, type="equal", value="Hidden"
    )

    # Public View 2 has filters which match row 2
    data_fixture.create_view_filter(
        view=public_view2, field=visible_field, type="equal", value="Visible"
    )
    data_fixture.create_view_filter(
        view=public_view2, field=hidden_field, type="equal", value="Not Match"
    )

    # Public View 3 has filters which match both rows
    data_fixture.create_view_filter(
        view=public_view2, field=visible_field, type="equal", value="Visible"
    )

    row = RowHandler().create_row(
        user=user,
        table=table,
        values={
            f"field_{visible_field.id}": "Visible",
            f"field_{hidden_field.id}": "Hidden",
        },
    )
    row2 = RowHandler().create_row(
        user=user,
        table=table,
        values={
            f"field_{visible_field.id}": "Visible",
            f"field_{hidden_field.id}": "Not Match",
        },
    )

    model = table.get_model()
    checker = ViewHandler().get_public_views_row_checker(
        table, model, only_include_views_which_want_realtime_events=True
    )

    assert checker.get_public_views_where_rows_are_visible([row, row2]) == [
        PublicViewRows(
            view=ViewHandler().get_view_as_user(user, public_view1.id).specific,
            allowed_row_ids={1},
        ),
        PublicViewRows(
            view=ViewHandler().get_view_as_user(user, public_view2.id).specific,
            allowed_row_ids={2},
        ),
        PublicViewRows(
            view=ViewHandler().get_view_as_user(user, public_view3.id).specific,
            allowed_row_ids=PublicViewRows.ALL_ROWS_ALLOWED,
        ),
    ]


@pytest.mark.django_db
def test_public_view_row_checker_caches_when_only_unfiltered_fields_updated(
    data_fixture, django_assert_num_queries
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    filtered_field = data_fixture.create_text_field(table=table)
    unfiltered_field = data_fixture.create_text_field(table=table)
    public_grid_view = data_fixture.create_grid_view(
        user,
        table=table,
        public=True,
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)

    data_fixture.create_view_filter(
        view=public_grid_view, field=filtered_field, type="equal", value="FilterValue"
    )
    model = table.get_model()
    visible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "FilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    invisible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "NotFilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    row_checker = ViewHandler().get_public_views_row_checker(
        table,
        model,
        only_include_views_which_want_realtime_events=True,
        updated_field_ids=[unfiltered_field.id],
    )

    assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
        public_grid_view.view_ptr.specific
    ]
    assert row_checker.get_public_views_where_row_is_visible(invisible_row) == []

    # Because we've already checked these rows and we've told the checker we'll only
    # be changing unfiltered_field it knows it can cache the results
    with django_assert_num_queries(0):
        assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
            public_grid_view.view_ptr.specific
        ]
        assert row_checker.get_public_views_where_row_is_visible(invisible_row) == []


@pytest.mark.django_db
def test_public_view_row_checker_includes_public_views_with_no_filters_with_no_queries(
    data_fixture, django_assert_num_queries
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    filtered_field = data_fixture.create_text_field(table=table)
    unfiltered_field = data_fixture.create_text_field(table=table)
    public_grid_view = data_fixture.create_grid_view(
        user,
        table=table,
        public=True,
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)

    model = table.get_model()
    visible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "any",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    other_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "any",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    row_checker = ViewHandler().get_public_views_row_checker(
        table,
        model,
        only_include_views_which_want_realtime_events=True,
        updated_field_ids=[unfiltered_field.id],
    )

    view_ptr_specific = public_grid_view.view_ptr.specific
    # It should precalculate that this view is always visible.
    with django_assert_num_queries(0):
        assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
            view_ptr_specific
        ]
        assert row_checker.get_public_views_where_row_is_visible(other_row) == [
            view_ptr_specific
        ]


@pytest.mark.django_db
def test_public_view_row_checker_does_not_cache_when_any_filtered_fields_updated(
    data_fixture, django_assert_num_queries
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    filtered_field = data_fixture.create_text_field(table=table)
    unfiltered_field = data_fixture.create_text_field(table=table)
    public_grid_view = data_fixture.create_grid_view(
        user,
        table=table,
        public=True,
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)

    data_fixture.create_view_filter(
        view=public_grid_view, field=filtered_field, type="equal", value="FilterValue"
    )
    model = table.get_model()
    visible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "FilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    invisible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "NotFilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    row_checker = ViewHandler().get_public_views_row_checker(
        table,
        model,
        only_include_views_which_want_realtime_events=True,
        updated_field_ids=[filtered_field.id, unfiltered_field.id],
    )

    assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
        public_grid_view.view_ptr.specific
    ]
    assert row_checker.get_public_views_where_row_is_visible(invisible_row) == []

    # Now update the rows so they swap and the invisible one becomes visible and vice
    # versa
    setattr(invisible_row, f"field_{filtered_field.id}", "FilterValue")
    invisible_row.save()
    setattr(visible_row, f"field_{filtered_field.id}", "NotFilterValue")
    visible_row.save()

    assert row_checker.get_public_views_where_row_is_visible(invisible_row) == [
        public_grid_view.view_ptr.specific
    ]
    assert row_checker.get_public_views_where_row_is_visible(visible_row) == []


@pytest.mark.django_db
def test_public_view_row_checker_runs_expected_queries_on_init(
    data_fixture, django_assert_num_queries
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    filtered_field = data_fixture.create_text_field(table=table)
    unfiltered_field = data_fixture.create_text_field(table=table)
    public_grid_view = data_fixture.create_grid_view(
        user, table=table, public=True, order=0
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)

    data_fixture.create_view_filter(
        view=public_grid_view, field=filtered_field, type="equal", value="FilterValue"
    )
    model = table.get_model()
    num_queries = 6
    with django_assert_num_queries(num_queries):
        # First query to get the public views, second query to get their filters.
        ViewHandler().get_public_views_row_checker(
            table,
            model,
            only_include_views_which_want_realtime_events=True,
            updated_field_ids=[filtered_field.id, unfiltered_field.id],
        )

    another_public_grid_view = data_fixture.create_grid_view(
        user, table=table, public=True, order=1
    )
    # Public View 1 has filters which match row 1
    data_fixture.create_view_filter(
        view=another_public_grid_view,
        field=filtered_field,
        type="equal",
        value="FilterValue",
    )

    # Adding another view shouldn't result in more queries
    with django_assert_num_queries(num_queries):
        # First query to get the public views, second query to get their filters.
        ViewHandler().get_public_views_row_checker(
            table,
            model,
            only_include_views_which_want_realtime_events=True,
            updated_field_ids=[filtered_field.id, unfiltered_field.id],
        )


@pytest.mark.django_db
def test_public_view_row_checker_runs_expected_queries_when_checking_rows(
    data_fixture, django_assert_num_queries
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    filtered_field = data_fixture.create_text_field(table=table)
    unfiltered_field = data_fixture.create_text_field(table=table)
    public_grid_view = data_fixture.create_grid_view(
        user, table=table, public=True, order=0
    )
    # Should not appear in any results
    data_fixture.create_form_view(user, table=table, public=True)

    # Public View 1 has filters which match row 1
    data_fixture.create_view_filter(
        view=public_grid_view, field=filtered_field, type="equal", value="FilterValue"
    )
    model = table.get_model()
    visible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "FilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    invisible_row = model.objects.create(
        **{
            f"field_{filtered_field.id}": "NotFilterValue",
            f"field_{unfiltered_field.id}": "any",
        }
    )
    row_checker = ViewHandler().get_public_views_row_checker(
        table,
        model,
        only_include_views_which_want_realtime_events=True,
        updated_field_ids=[filtered_field.id, unfiltered_field.id],
    )

    view_ptr_specific = public_grid_view.view_ptr.specific
    with django_assert_num_queries(1):
        # Only should run a single exists query to check if the row is in the single
        # public view
        assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
            view_ptr_specific
        ]
    with django_assert_num_queries(1):
        # Only should run a single exists query to check if the row is in the single
        # public view
        assert row_checker.get_public_views_where_row_is_visible(invisible_row) == []

    another_public_grid_view = data_fixture.create_grid_view(
        user, table=table, public=True, order=1
    )
    data_fixture.create_view_filter(
        view=another_public_grid_view,
        field=filtered_field,
        type="equal",
        value="FilterValue",
    )

    row_checker = ViewHandler().get_public_views_row_checker(
        table,
        model,
        only_include_views_which_want_realtime_events=True,
        updated_field_ids=[filtered_field.id, unfiltered_field.id],
    )
    specific_another_view = another_public_grid_view.view_ptr.specific
    with django_assert_num_queries(2):
        # Now should run two queries, one per public view
        assert row_checker.get_public_views_where_row_is_visible(visible_row) == [
            view_ptr_specific,
            specific_another_view,
        ]
    with django_assert_num_queries(2):
        # Now should run two queries, one per public view
        assert row_checker.get_public_views_where_row_is_visible(invisible_row) == []


@pytest.mark.django_db
def test_cant_get_view_filter_when_view_trashed(data_fixture):
    user = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    view_filter = data_fixture.create_view_filter(user=user, view=grid_view)

    ViewHandler().delete_view(user, grid_view)

    with pytest.raises(ViewFilterDoesNotExist):
        ViewHandler().get_filter(user, view_filter.id)


@pytest.mark.django_db
def test_cant_apply_sorting_when_view_trashed(data_fixture):
    user = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)

    ViewHandler().delete_view(user, grid_view)

    with pytest.raises(ViewSortDoesNotExist):
        ViewHandler().apply_sorting(
            grid_view,
            grid_view.table.get_model().objects.all(),
        )


@pytest.mark.django_db
def test_cant_get_sort_when_view_trashed(data_fixture):
    user = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    field = data_fixture.create_number_field(user, table=grid_view.table)

    view_sort = ViewHandler().create_sort(user, grid_view, field, "asc")
    ViewHandler().delete_view(user, grid_view)

    with pytest.raises(ViewSortDoesNotExist):
        ViewHandler().get_sort(user, view_sort.id)


@pytest.mark.django_db
def test_cant_update_sort_when_view_trashed(data_fixture):
    user = data_fixture.create_user()
    grid_view = data_fixture.create_grid_view(user=user)
    field = data_fixture.create_number_field(user, table=grid_view.table)

    view_sort = ViewHandler().create_sort(user, grid_view, field, "asc")
    ViewHandler().delete_view(user, grid_view)

    with pytest.raises(ViewSortDoesNotExist):
        ViewHandler().update_sort(user, view_sort, field)


@pytest.mark.django_db
def test_get_public_rows_queryset_and_field_ids_view_filters_applied(data_fixture):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 1})
    model.objects.create(**{f"field_{field.id}": 2})
    model.objects.create(**{f"field_{field.id}": 3})

    data_fixture.create_view_filter(view=grid_view, field=field, type="equal", value=1)

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(grid_view)

    assert queryset.count() == 1
    assert list(field_ids) == [field.id]


@pytest.mark.django_db
@pytest.mark.parametrize("search_mode", ALL_SEARCH_MODES)
def test_get_public_rows_queryset_and_field_ids_view_search(data_fixture, search_mode):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 4})
    model.objects.create(**{f"field_{field.id}": 5})
    model.objects.create(**{f"field_{field.id}": 6})

    SearchHandler.update_tsvector_columns(
        field.table, update_tsvectors_for_changed_rows_only=False
    )

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view, search="5", search_mode=search_mode
    )

    assert queryset.count() == 1
    assert list(queryset.values_list(f"field_{field.id}", flat=True)) == [Decimal(5)]


@pytest.mark.django_db
def test_get_public_rows_queryset_and_field_ids_view_order_by(data_fixture):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 1})
    model.objects.create(**{f"field_{field.id}": 2})
    model.objects.create(**{f"field_{field.id}": 3})

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view, order_by=f"-field_{field.id}"
    )

    assert queryset.count() == 3
    assert list(queryset.values_list(f"field_{field.id}", flat=True)) == [3, 2, 1]

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view, order_by=f"field_{field.id}"
    )

    assert queryset.count() == 3
    assert list(queryset.values_list(f"field_{field.id}", flat=True)) == [1, 2, 3]


@pytest.mark.django_db
def test_get_public_rows_queryset_and_field_ids_include_exclude_fields(data_fixture):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)
    field_two = data_fixture.create_text_field(table=grid_view.table)

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 1})
    model.objects.create(**{f"field_{field.id}": 2})
    model.objects.create(**{f"field_{field.id}": 3})

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view,
        include_fields="field_" + str(field.id),
        exclude_fields="field_" + str(field_two.id),
    )

    assert queryset.count() == 3
    assert field_ids == [field.id]


@pytest.mark.django_db
def test_get_public_rows_queryset_and_field_ids_filter(data_fixture):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 1})
    model.objects.create(**{f"field_{field.id}": 2})
    model.objects.create(**{f"field_{field.id}": 3})

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view,
        filter_object={f"filter__field_{field.id}__equal": "2"},
    )

    assert queryset.count() == 1
    assert list(queryset.values_list(f"field_{field.id}", flat=True)) == [2]


@pytest.mark.django_db
def test_get_public_rows_queryset_and_field_ids_filters_stack(data_fixture):
    grid_view = data_fixture.create_grid_view(public=True)
    field = data_fixture.create_number_field(table=grid_view.table)
    field_2 = data_fixture.create_text_field(table=grid_view.table)

    data_fixture.create_view_filter(
        view=grid_view, field=field_2, type="equal", value="b"
    )

    model = grid_view.table.get_model()
    model.objects.create(**{f"field_{field.id}": 2, f"field_{field_2.id}": "a"})
    model.objects.create(**{f"field_{field.id}": 2, f"field_{field_2.id}": "b"})
    model.objects.create(**{f"field_{field.id}": 3, f"field_{field_2.id}": "c"})

    (
        queryset,
        field_ids,
        publicly_visible_field_options,
    ) = ViewHandler().get_public_rows_queryset_and_field_ids(
        grid_view, filter_object={f"field_{field.id}": 2}
    )

    assert queryset.count() == 1
    assert list(queryset.values_list(f"field_{field.id}", flat=True)) == [2]


@pytest.mark.django_db
def test_can_submit_form_view_handler_with_zero_number_required(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    number_field = data_fixture.create_number_field(table=table)
    data_fixture.create_form_view_field_option(
        form, number_field, required=True, enabled=True
    )

    handler = ViewHandler()

    handler.submit_form_view(user, form=form, values={f"field_{number_field.id}": 0})
    with pytest.raises(ValidationError):
        handler.submit_form_view(
            user, form=form, values={f"field_{number_field.id}": False}
        )


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_list_views_ownership_type(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    handler = ViewHandler()
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view2 = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view2.ownership_type = "personal"
    view2.save()

    result = handler.list_views(user, table, "grid", None, None, None, 10)
    assert len(result) == 1


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_get_view_ownership_type(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    handler = ViewHandler()

    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )

    grid = handler.get_view_as_user(user, view.id)
    assert grid.ownership_type == OWNERSHIP_TYPE_COLLABORATIVE

    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.get_view_as_user(user, view.id)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_create_view_ownership_type(data_fixture):
    ownership_types = {"collaborative": CollaborativeViewOwnershipType()}

    with patch(
        "baserow.contrib.database.views.registries.view_ownership_type_registry.registry",
        ownership_types,
    ):
        user = data_fixture.create_user()
        table = data_fixture.create_database_table(user=user)
        handler = ViewHandler()

        view = handler.create_view(
            user=user,
            table=table,
            type_name="grid",
            name="Test grid",
            ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
        )

        grid = GridView.objects.first()
        assert grid.ownership_type == OWNERSHIP_TYPE_COLLABORATIVE

        with pytest.raises(ViewOwnershipTypeDoesNotExist):
            handler.create_view(
                user=user,
                table=table,
                type_name="grid",
                name="grid",
                ownership_type="personal",
            )


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_update_view_ownership_type(data_fixture):
    """
    Updating view.ownership_type is currently not allowed.
    """

    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    form = data_fixture.create_form_view(table=table)
    handler = ViewHandler()

    handler.update_view(user=user, view=form, ownership_type="new_ownership_type")

    form.refresh_from_db()
    assert form.ownership_type == OWNERSHIP_TYPE_COLLABORATIVE


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_duplicate_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()

    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )

    duplicated = handler.duplicate_view(user2, view)
    assert duplicated.ownership_type == OWNERSHIP_TYPE_COLLABORATIVE

    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.duplicate_view(user, view)

    with pytest.raises(PermissionDenied):
        handler.duplicate_view(user2, view)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_delete_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()

    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.delete_view(user, view)

    with pytest.raises(PermissionDenied):
        handler.delete_view(user2, view)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_field_options_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()

    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.get_field_options_as_user(user, view)

    with pytest.raises(PermissionDenied):
        handler.get_field_options_as_user(user2, view)

    with pytest.raises(PermissionDenied):
        handler.update_field_options(view, {}, user)

    with pytest.raises(PermissionDenied):
        handler.update_field_options(view, {}, user2)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_filters_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()

    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    field = data_fixture.create_text_field(table=view.table)
    filter = handler.create_filter(user, view, field, "equal", "value")
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.create_filter(user, view, field, "equal", "value")

    with pytest.raises(PermissionDenied):
        handler.create_filter(user2, view, field, "equal", "value")

    with pytest.raises(PermissionDenied):
        handler.get_filter(user, filter.id)

    with pytest.raises(PermissionDenied):
        handler.get_filter(user2, filter.id)

    with pytest.raises(PermissionDenied):
        handler.list_filters(user, view.id)

    with pytest.raises(PermissionDenied):
        handler.list_filters(user2, view.id)

    with pytest.raises(PermissionDenied):
        handler.update_filter(user, filter, field, "equal", "another value")

    with pytest.raises(PermissionDenied):
        handler.update_filter(user2, filter, field, "equal", "another value")

    with pytest.raises(PermissionDenied):
        handler.delete_filter(user, filter)

    with pytest.raises(PermissionDenied):
        handler.delete_filter(user2, filter)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_sorts_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    field = data_fixture.create_text_field(table=view.table)
    equal_sort = data_fixture.create_view_sort(user=user, view=view, field=field)
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.create_sort(user=user, view=view, field=field, order="ASC")

    with pytest.raises(PermissionDenied):
        handler.create_sort(user=user2, view=view, field=field, order="ASC")

    with pytest.raises(PermissionDenied):
        handler.get_sort(user, equal_sort.id)

    with pytest.raises(PermissionDenied):
        handler.get_sort(user2, equal_sort.id)

    with pytest.raises(PermissionDenied):
        handler.list_sorts(user, view.id)

    with pytest.raises(PermissionDenied):
        handler.list_sorts(user2, view.id)

    with pytest.raises(PermissionDenied):
        handler.update_sort(user, equal_sort, field)

    with pytest.raises(PermissionDenied):
        handler.update_sort(user2, equal_sort, field)

    with pytest.raises(PermissionDenied):
        handler.delete_sort(user, equal_sort)

    with pytest.raises(PermissionDenied):
        handler.delete_sort(user2, equal_sort)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_decorations_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    decorator_type_name = "left_border_color"
    value_provider_type_name = ""
    value_provider_conf = {}
    decoration = data_fixture.create_view_decoration(user=user, view=view)
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.create_decoration(
            view,
            decorator_type_name,
            value_provider_type_name,
            value_provider_conf,
            user=user,
        )

    with pytest.raises(PermissionDenied):
        handler.create_decoration(
            view,
            decorator_type_name,
            value_provider_type_name,
            value_provider_conf,
            user=user2,
        )

    with pytest.raises(PermissionDenied):
        handler.get_decoration(user, decoration.id)

    with pytest.raises(PermissionDenied):
        handler.get_decoration(user2, decoration.id)

    with pytest.raises(PermissionDenied):
        handler.list_decorations(user, view.id)

    with pytest.raises(PermissionDenied):
        handler.list_decorations(user2, view.id)

    with pytest.raises(PermissionDenied):
        handler.update_decoration(decoration, user)

    with pytest.raises(PermissionDenied):
        handler.update_decoration(decoration, user2)

    with pytest.raises(PermissionDenied):
        handler.delete_decoration(decoration, user)

    with pytest.raises(PermissionDenied):
        handler.delete_decoration(decoration, user2)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_aggregations_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()
    field = data_fixture.create_number_field(user=user, table=table)
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    handler.update_field_options(
        view=view,
        field_options={
            field.id: {
                "aggregation_type": "sum",
                "aggregation_raw_type": "sum",
            }
        },
    )
    aggr = [
        (
            field,
            "max",
        ),
    ]

    handler.get_view_field_aggregations(user, view)
    handler.get_field_aggregations(user, view, aggr)

    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.get_view_field_aggregations(user, view)

    with pytest.raises(PermissionDenied):
        handler.get_view_field_aggregations(user2, view)

    with pytest.raises(PermissionDenied):
        handler.get_field_aggregations(user, view, aggr)

    with pytest.raises(PermissionDenied):
        handler.get_field_aggregations(user2, view, aggr)


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_update_view_slug_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()
    field = data_fixture.create_number_field(user=user, table=table)
    view = handler.create_view(
        user=user,
        table=table,
        type_name="form",
        name="Form",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view.ownership_type = "personal"
    view.save()

    with pytest.raises(PermissionDenied):
        handler.update_view_slug(user, view, "new-slug")

    with pytest.raises(PermissionDenied):
        handler.update_view_slug(user2, view, "new-slug")


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_get_public_view_ownership_type(data_fixture):
    workspace = data_fixture.create_workspace(name="Workspace 1")
    user = data_fixture.create_user(workspace=workspace)
    user2 = data_fixture.create_user(workspace=workspace)
    database = data_fixture.create_database_application(workspace=workspace)
    table = data_fixture.create_database_table(user=user, database=database)
    handler = ViewHandler()
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view.ownership_type = "personal"
    view.public = False
    view.slug = "slug"
    view.save()

    with pytest.raises(ViewDoesNotExist):
        handler.get_public_view_by_slug(user, "slug")

    with pytest.raises(ViewDoesNotExist):
        handler.get_public_view_by_slug(user2, "slug")

    view.public = True
    view.save()

    handler.get_public_view_by_slug(user, "slug")


@pytest.mark.django_db
@pytest.mark.view_ownership
def test_order_views_ownership_type(data_fixture):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    handler = ViewHandler()
    view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view2 = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view2.ownership_type = "personal"
    view2.save()

    handler.order_views(user, table, [view.id])

    with pytest.raises(ViewNotInTable):
        handler.order_views(user, table, [view2.id])


@override_settings(AUTO_INDEX_VIEW_ENABLED=True)
@pytest.mark.django_db(transaction=True)
def test_creating_view_sort_creates_a_new_index(data_fixture, enable_singleton_testing):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )

    table_model = table.get_model()
    # without any sort the index key should be None
    index = ViewIndexingHandler.get_index(grid_view, table_model)
    assert index is None

    # after creating a sort the index key should be set and the index should be
    # created in the database.
    handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    index = ViewIndexingHandler.get_index(grid_view, table_model)

    assert ViewIndexingHandler.does_index_exist(index.name) is True


@override_settings(
    AUTO_INDEX_VIEW_ENABLED=True,
)
@pytest.mark.django_db(transaction=True)
def test_updating_view_sorts_creates_a_new_index_and_delete_the_unused_one(
    data_fixture, enable_singleton_testing
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    number_field = data_fixture.create_number_field(user=user, table=table)
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )

    table_model = table.get_model()
    # creating a view_sort should create a new index.
    view_sort_1 = handler.create_sort(
        user=user, view=grid_view, field=text_field, order="ASC"
    )
    index_1 = ViewIndexingHandler.get_index(grid_view, table_model)
    assert ViewIndexingHandler.does_index_exist(index_1.name) is True

    # Adding a new view_sort should create a new index and delete the previous one.
    view_sort_2 = handler.create_sort(
        user=user, view=grid_view, field=number_field, order="ASC"
    )
    assert ViewIndexingHandler.does_index_exist(index_1.name) is False

    index_2 = ViewIndexingHandler.get_index(grid_view, table_model)
    assert ViewIndexingHandler.does_index_exist(index_2.name) is True

    # updating the sort should create a new index and delete the previous one.
    handler.update_sort(user, view_sort_1, field=text_field, order="DESC")
    assert ViewIndexingHandler.does_index_exist(index_2.name) is False

    index_3 = ViewIndexingHandler.get_index(grid_view, table_model)
    assert ViewIndexingHandler.does_index_exist(index_3.name) is True

    # removing the first view_sort should create a new index and delete the
    # previous one.
    handler.delete_sort(user, view_sort_2)
    assert ViewIndexingHandler.does_index_exist(index_3.name) is False

    index_4 = ViewIndexingHandler.get_index(grid_view, table_model)
    assert ViewIndexingHandler.does_index_exist(index_4.name) is True

    # deleting the last view_sort should delete the last index.
    handler.delete_sort(user, view_sort_1)
    index_none = ViewIndexingHandler.get_index(grid_view, table_model)
    assert index_none is None

    assert ViewIndexingHandler.does_index_exist(index_4.name) is False


@override_settings(AUTO_INDEX_VIEW_ENABLED=True)
@pytest.mark.django_db(transaction=True)
def test_perm_deleting_view_remove_index_if_unused(
    data_fixture, enable_singleton_testing
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    database = table.database
    workspace = database.workspace
    text_field = data_fixture.create_text_field(user=user, table=table)
    table_model = table.get_model()
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    grid_view.refresh_from_db()
    assert ViewIndexingHandler.does_index_exist(grid_view.db_index_name) is True

    # duplicate the view with the same sorting
    grid_view_2 = handler.duplicate_view(user, grid_view)
    index = ViewIndexingHandler.get_index(grid_view, table_model)

    # permanently delete the second view is not going to delete the index because
    # it is still used by the first view.
    trash_handler = TrashHandler()
    trash_handler.trash(user, workspace, database, grid_view_2)
    trash_handler.permanently_delete(grid_view_2)

    assert ViewIndexingHandler.does_index_exist(index.name) is True

    # permanently delete the first view is going to delete the index because
    # it is not used anymore.
    trash_handler.trash(user, workspace, database, grid_view)
    trash_handler.permanently_delete(grid_view)
    assert ViewIndexingHandler.does_index_exist(index.name) is False


@override_settings(AUTO_INDEX_VIEW_ENABLED=True)
@pytest.mark.django_db(transaction=True)
def test_duplicating_table_do_not_duplicate_indexes(
    data_fixture, enable_singleton_testing
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    index = ViewIndexingHandler.get_index(grid_view, table.get_model())

    # duplicating the table will not duplicate the index.
    table_2 = TableHandler().duplicate_table(user, table)
    grid_view_2 = table_2.view_set.get()
    index_2 = ViewIndexingHandler.get_index(grid_view_2, table_2.get_model())

    assert index is not None
    assert index_2 is not None
    assert index.name != index_2.name
    assert ViewIndexingHandler.does_index_exist(index.name) is True
    assert ViewIndexingHandler.does_index_exist(index_2.name) is False


@override_settings(AUTO_INDEX_VIEW_ENABLED=True)
@pytest.mark.django_db(transaction=True)
def test_deleting_a_field_of_a_view_sort_update_view_indexes(
    data_fixture, enable_singleton_testing
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    text_field_2 = data_fixture.create_text_field(user=user, table=table)
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    index = ViewIndexingHandler.get_index(grid_view, table.get_model())
    assert ViewIndexingHandler.does_index_exist(index.name) is True

    # deleting the field without a view_sort should not delete the index
    FieldHandler().delete_field(user, text_field_2)
    assert ViewIndexingHandler.does_index_exist(index.name) is True

    # deleting the field with a view_sort should delete the index
    FieldHandler().delete_field(user, text_field)
    assert ViewIndexingHandler.does_index_exist(index.name) is False


@override_settings(AUTO_INDEX_VIEW_ENABLED=True)
@pytest.mark.django_db(transaction=True)
def test_changing_a_field_type_of_a_view_sort_to_non_orderable_one_delete_view_index(
    data_fixture, enable_singleton_testing
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    handler = ViewHandler()
    grid_view = handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")
    index = ViewIndexingHandler.get_index(grid_view, table.get_model())
    assert ViewIndexingHandler.does_index_exist(index.name) is True

    FieldHandler().update_field(
        user,
        text_field,
        new_type_name="link_row",
        link_row_table=table,
        has_related_field=False,
    )
    assert ViewIndexingHandler.does_index_exist(index.name) is False


@patch("baserow.contrib.database.views.tasks.update_view_index.delay")
@pytest.mark.django_db(transaction=True)
def test_loading_a_view_checks_for_db_index_without_additional_queries(
    mocked_view_index_update_task,
    data_fixture,
    enable_singleton_testing,
    django_assert_num_queries,
):
    user = data_fixture.create_user()
    table = data_fixture.create_database_table(user=user)
    text_field = data_fixture.create_text_field(user=user, table=table)
    view_handler = ViewHandler()
    grid_view = view_handler.create_view(
        user=user,
        table=table,
        type_name="grid",
        name="Test grid",
        ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
    )
    view_handler.create_sort(user=user, view=grid_view, field=text_field, order="ASC")

    model = table.get_model()

    grid_view.refresh_from_db()
    assert not grid_view.db_index_name

    view = view_handler.get_view(
        grid_view.id, base_queryset=GridView.objects.prefetch_related("viewsort_set")
    )

    with django_assert_num_queries(0):
        ViewIndexingHandler.schedule_index_creation_if_needed(view, model)

    assert mocked_view_index_update_task.call_count == 0

    with override_settings(AUTO_INDEX_VIEW_ENABLED=True), django_assert_num_queries(0):
        ViewIndexingHandler.schedule_index_creation_if_needed(view, model)
        assert mocked_view_index_update_task.call_count == 1

    # actually create the index for the view
    ViewIndexingHandler.update_index(grid_view, model)
    view = view_handler.get_view(
        grid_view.id, base_queryset=GridView.objects.prefetch_related("viewsort_set")
    )
    assert view.db_index_name

    with override_settings(AUTO_INDEX_VIEW_ENABLED=True), django_assert_num_queries(0):
        ViewIndexingHandler.schedule_index_creation_if_needed(view, model)
        # the task should not be called again
        assert mocked_view_index_update_task.call_count == 1


@override_settings(
    AUTO_INDEX_VIEW_ENABLED=True,
)
@pytest.mark.django_db(transaction=True)
def test_update_index_replaces_index_with_diff_collation(
    settings, data_fixture, enable_singleton_testing
):
    with patch("baserow.core.db.get_collation_name", new=lambda: None):
        user = data_fixture.create_user()
        table = data_fixture.create_database_table(user=user)
        text_field = data_fixture.create_text_field(user=user, table=table)
        handler = ViewHandler()
        grid_view = handler.create_view(
            user=user,
            table=table,
            type_name="grid",
            name="Test grid",
            ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
        )
        table_model = table.get_model()
        view_sort_1 = handler.create_sort(
            user=user, view=grid_view, field=text_field, order="ASC"
        )
        index_1 = ViewIndexingHandler.get_index(grid_view, table_model)
        assert ViewIndexingHandler.does_index_exist(index_1.name) is True
        grid_view.refresh_from_db()

    with patch(
        "baserow.core.db.get_collation_name", new=lambda: settings.EXPECTED_COLLATION
    ):
        # different collation settings should overwrite the index
        ViewIndexingHandler.update_index(grid_view, table_model)

        index_2 = ViewIndexingHandler.get_index(grid_view, table_model)
        assert index_1.name != index_2.name
        assert ViewIndexingHandler.does_index_exist(index_1.name) is False
        assert ViewIndexingHandler.does_index_exist(index_2.name) is True


@override_settings(
    AUTO_INDEX_VIEW_ENABLED=True,
)
@pytest.mark.django_db(transaction=True)
def test_view_loaded_replaces_index_with_diff_collation(
    settings, data_fixture, enable_singleton_testing
):
    with patch("baserow.core.db.get_collation_name", new=lambda: None):
        user = data_fixture.create_user()
        table = data_fixture.create_database_table(user=user)
        text_field = data_fixture.create_text_field(user=user, table=table)
        handler = ViewHandler()
        grid_view = handler.create_view(
            user=user,
            table=table,
            type_name="grid",
            name="Test grid",
            ownership_type=OWNERSHIP_TYPE_COLLABORATIVE,
        )
        table_model = table.get_model()
        view_sort_1 = handler.create_sort(
            user=user, view=grid_view, field=text_field, order="ASC"
        )
        index_1 = ViewIndexingHandler.get_index(grid_view, table_model)
        assert ViewIndexingHandler.does_index_exist(index_1.name) is True
        grid_view.refresh_from_db()

    with patch(
        "baserow.core.db.get_collation_name", new=lambda: settings.EXPECTED_COLLATION
    ):
        # different collation settings should overwrite the index
        view_loaded.send(
            sender=None,
            table=table,
            view=grid_view,
            table_model=table_model,
            user=user,
        )

        index_2 = ViewIndexingHandler.get_index(grid_view, table_model)
        assert index_1.name != index_2.name
        assert ViewIndexingHandler.does_index_exist(index_1.name) is False
        assert ViewIndexingHandler.does_index_exist(index_2.name) is True
