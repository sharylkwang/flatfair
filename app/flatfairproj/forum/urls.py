from django.urls import path



from . import views



urlpatterns = [
    path("community", views.community, name="community"),


    path("community/ask", views.ask, name="community_ask"),



    path("community/<int:pk>", views.question, name="community_question"),



    path("community/<int:pk>/edit", views.edit, name="community_edit"),


    path("community/member/<int:pk>", views.member, name="community_member"),



    path("community/<int:pk>/vote", views.vote, name="community_vote"),



    path("community/<int:pk>/report", views.report, name="community_report"),



    path("staff/community", views.moderation, name="admin_community"),



    path("staff/community/<int:pk>", views.moderate, name="admin_community_action"),


    path(


        "staff/community/<int:pk>/check",
        views.judge_post,
        name="admin_community_post",
    ),
]
