from django.urls import path

from . import views

app_name = "chat"

urlpatterns = [
    path("chat/", views.chat_home, name="home"),
    path("chat/start/", views.start_conversation, name="start"),
    path("chat/<int:conversation_id>/", views.chat_home, name="conversation"),
    path("chat/<int:conversation_id>/send/", views.send_message, name="send"),
    path("runs/<int:run_id>/stream/", views.stream_run, name="stream"),
    path("runs/<int:run_id>/", views.run_trace, name="trace"),
    path(
        "actions/<uuid:proposal_uuid>/<str:action>/",
        views.decide_proposal,
        name="decide",
    ),
]
