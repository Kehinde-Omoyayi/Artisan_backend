from django.urls import path
from . import views

urlpatterns = [
    path('new/', views.review_create, name='review-create'),
    path('thanks/', views.review_thanks, name='review-thanks'),
    path('improvement/', views.review_improvement, name='review-improvement'),
]
