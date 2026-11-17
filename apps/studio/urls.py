from django.urls import path

from . import agent_views as a
from . import views

app_name = "studio"

urlpatterns = [
    # agents (spec §7)
    path("", a.dashboard, name="home"),
    path("agents/new/", a.agent_create, name="agent_create"),
    path("agents/<slug:slug>/clone/", a.agent_clone, name="agent_clone"),
    path("agents/<slug:slug>/playground/", a.playground, name="playground"),
    path("agents/<slug:slug>/playground/start/", a.playground_start, name="playground_start"),
    path("agents/<slug:slug>/playground/trigger/", a.playground_trigger, name="playground_trigger"),
    path(
        "agents/<slug:slug>/playground/<int:conversation_id>/send/",
        a.playground_send,
        name="playground_send",
    ),
    path("agents/<slug:slug>/tests/", a.tests, name="tests"),
    path("agents/<slug:slug>/tests/new/", a.test_case_edit, name="test_case_new"),
    path("agents/<slug:slug>/tests/run/", a.tests_run, name="tests_run"),
    path("agents/<slug:slug>/tests/<int:case_id>/", a.test_case_edit, name="test_case_edit"),
    path("agents/<slug:slug>/publish/", a.publish, name="publish"),
    path("agents/<slug:slug>/versions/", a.versions, name="versions"),
    path("agents/<slug:slug>/versions/<int:number>/restore/", a.version_restore, name="restore"),
    path("agents/<slug:slug>/analytics/", a.analytics, name="analytics"),
    path("agents/<slug:slug>/<str:section>/", a.agent_section, name="agent_section"),
    path("runs/<int:run_id>/", a.run_detail, name="run"),
    # tool catalog (Phase 2)
    path("tools/", views.tool_list, name="tools"),
    path("tools/new/", views.tool_create, name="tool_create"),
    path("tools/<str:key>/", views.tool_detail, name="tool_detail"),
    path("tools/<str:key>/edit/", views.tool_edit, name="tool_edit"),
    path("tools/<str:key>/toggle/", views.tool_toggle, name="tool_toggle"),
    path("tools/<str:key>/test/", views.tool_test, name="tool_test"),
]
