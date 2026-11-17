from django.urls import path

from . import views

app_name = "runs"

urlpatterns = [
    path("runs/", views.run_list, name="list"),
    path("approvals/", views.approvals, name="approvals"),
    path("approvals/history/", views.approval_history, name="approval_history"),
    path("approvals/bulk/", views.bulk_decide, name="bulk"),
]
