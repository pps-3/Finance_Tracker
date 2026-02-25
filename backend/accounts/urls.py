from django.urls import path
from . import views



urlpatterns = [
    
    path('login/', views.login, name='login'),
    path('token/refresh/', views.refresh_token_view, name='token_refresh'),
    path('logout/', views.logout, name='logout'),
    path('register/', views.RegisterView.as_view(), name='register'),
    path('profile/', views.UserProfileView.as_view(), name='user-profile'),
    path('user/', views.get_current_user, name='current-user'),
    path('change-password/', views.ChangePasswordView.as_view(), name='change-password'),
    path('users/', views.UserListView.as_view(), name='user-list'),
]

















# urlpatterns = [
#     # Authentication endpoints
#     path('register/', RegisterView.as_view(), name='register'),
#     path('login/', TokenObtainPairView.as_view(), name='login'),
#     path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
#     path('token/verify/', TokenVerifyView.as_view(), name='token_verify'),
    
#     # User management endpoints
#     path('profile/', UserProfileView.as_view(), name='profile'),
#     path('change-password/', ChangePasswordView.as_view(), name='change_password'),
#     path('users/', UserListView.as_view(), name='user_list'),
# ]
