# backend/accounts/views.py

from rest_framework import generics, status, permissions
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.exceptions import TokenError, InvalidToken
from django.contrib.auth import authenticate, get_user_model
from django.conf import settings

from .serializers import (
    UserRegistrationSerializer,
    UserSerializer,
    ChangePasswordSerializer
)

User = get_user_model()


# ============================================================
# HELPER FUNCTIONS FOR HTTPONLY COOKIES
# ============================================================

def set_auth_cookies(response, access_token, refresh_token):
    """
    Set secure httpOnly cookies for JWT tokens
    
    Security features:
    - HttpOnly: JavaScript cannot access (XSS protection)
    - Secure: Only sent over HTTPS (in production)
    - SameSite: CSRF protection
    """
    
    # Access token cookie
    response.set_cookie(
        key='access_token',
        value=access_token,
        max_age=settings.SIMPLE_JWT['ACCESS_TOKEN_LIFETIME'].total_seconds(),
        httponly=True,  # ✅ XSS protection
        secure=settings.SIMPLE_JWT.get('AUTH_COOKIE_SECURE', True),  # ✅ HTTPS only
        samesite=settings.SIMPLE_JWT.get('AUTH_COOKIE_SAMESITE', 'Lax'),  # ✅ CSRF protection
        domain=settings.SIMPLE_JWT.get('AUTH_COOKIE_DOMAIN'),
        path='/',
    )
    
    # Refresh token cookie
    response.set_cookie(
        key='refresh_token',
        value=refresh_token,
        max_age=settings.SIMPLE_JWT['REFRESH_TOKEN_LIFETIME'].total_seconds(),
        httponly=True,  # ✅ XSS protection
        secure=settings.SIMPLE_JWT.get('AUTH_COOKIE_SECURE', True),  # ✅ HTTPS only
        samesite=settings.SIMPLE_JWT.get('AUTH_COOKIE_SAMESITE', 'Lax'),  # ✅ CSRF protection
        domain=settings.SIMPLE_JWT.get('AUTH_COOKIE_DOMAIN'),
        path='/',
    )
    
    print("🔒 Secure httpOnly cookies set")


def clear_auth_cookies(response):
    """Clear authentication cookies on logout"""
    response.delete_cookie('access_token', path='/', domain=settings.SIMPLE_JWT.get('AUTH_COOKIE_DOMAIN'))
    response.delete_cookie('refresh_token', path='/', domain=settings.SIMPLE_JWT.get('AUTH_COOKIE_DOMAIN'))
    print("🔓 Auth cookies cleared")


# ============================================================
# AUTHENTICATION ENDPOINTS (WITH HTTPONLY COOKIES)
# ============================================================

@api_view(['POST'])
@permission_classes([AllowAny])
def login(request):
    """
    Login with httpOnly cookie tokens
    Replaces: TokenObtainPairView
    """
    email = request.data.get('email')
    password = request.data.get('password')
    
    if not email or not password:
        return Response(
            {'error': 'Please provide both email and password'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    # Authenticate user
    user = authenticate(request, username=email, password=password)
    
    if user is None:
        return Response(
            {'error': 'Invalid credentials'},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    if not user.is_active:
        return Response(
            {'error': 'Account is disabled'},
            status=status.HTTP_403_FORBIDDEN
        )
    
    # Generate JWT tokens
    refresh = RefreshToken.for_user(user)
    access_token = str(refresh.access_token)
    refresh_token = str(refresh)
    
    # Create response
    response = Response({
        'user': UserSerializer(user).data,
        'message': 'Login successful'
    }, status=status.HTTP_200_OK)
    
    # ✅ SECURE: Set tokens in httpOnly cookies
    set_auth_cookies(response, access_token, refresh_token)
    
    print(f"✅ User logged in: {user.email}")
    return response


@api_view(['POST'])
@permission_classes([AllowAny])
def refresh_token_view(request):
    """
    Refresh access token using httpOnly refresh cookie
    Replaces: TokenRefreshView
    """
    
    # Get refresh token from cookie
    refresh_token_value = request.COOKIES.get('refresh_token')
    
    if not refresh_token_value:
        return Response(
            {'error': 'Refresh token not found'},
            status=status.HTTP_401_UNAUTHORIZED
        )
    
    try:
        # Verify and decode refresh token
        refresh = RefreshToken(refresh_token_value)
        
        # Generate new access token
        access_token = str(refresh.access_token)
        
        # If rotation is enabled, get new refresh token
        if settings.SIMPLE_JWT.get('ROTATE_REFRESH_TOKENS', False):
            refresh.set_jti()
            refresh.set_exp()
            new_refresh_token = str(refresh)
        else:
            new_refresh_token = refresh_token_value
        
        # Create response
        response = Response({
            'message': 'Token refreshed successfully'
        }, status=status.HTTP_200_OK)
        
        # ✅ SECURE: Set new tokens in httpOnly cookies
        set_auth_cookies(response, access_token, new_refresh_token)
        
        print("✅ Token refreshed")
        return response
        
    except TokenError as e:
        return Response(
            {'error': 'Invalid or expired refresh token'},
            status=status.HTTP_401_UNAUTHORIZED
        )


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    Logout and clear httpOnly cookies
    """
    
    response = Response({
        'message': 'Logout successful'
    }, status=status.HTTP_200_OK)
    
    # ✅ SECURE: Clear auth cookies
    clear_auth_cookies(response)
    
    print(f"✅ User logged out: {request.user.email}")
    return response


# ============================================================
# REGISTRATION (WITH HTTPONLY COOKIES)
# ============================================================

class RegisterView(generics.CreateAPIView):
    """
    API endpoint for user registration
    Now returns httpOnly cookies instead of tokens in response body
    """
    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    serializer_class = UserRegistrationSerializer
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Generate JWT tokens for auto-login after registration
        refresh = RefreshToken.for_user(user)
        access_token = str(refresh.access_token)
        refresh_token = str(refresh)
        
        # Create response
        response = Response({
            'user': UserSerializer(user).data,
            'message': 'User registered successfully'
        }, status=status.HTTP_201_CREATED)
        
        # ✅ SECURE: Set tokens in httpOnly cookies (auto-login)
        set_auth_cookies(response, access_token, refresh_token)
        
        print(f"✅ User registered: {user.email}")
        return response


# ============================================================
# USER PROFILE & PASSWORD MANAGEMENT (UNCHANGED)
# ============================================================

class UserProfileView(generics.RetrieveUpdateAPIView):
    """
    API endpoint for viewing and updating user profile
    Works with httpOnly cookies through middleware
    """
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]
    
    #generally youll use default /auth/profile/<pk> but here you overrode to return current logged-in user
    def get_object(self):           #By overriding get_object(),you told DRF:Don’t fetch by ID. Just return whoever is logged in."
        return self.request.user    


class ChangePasswordView(APIView):
    """
    API endpoint for changing password
    Works with httpOnly cookies through middleware
    """
    permission_classes = [permissions.IsAuthenticated]
    
    def post(self, request):
        serializer = ChangePasswordSerializer(data=request.data)
        
        if serializer.is_valid():
            user = request.user
            
            # Check old password
            if not user.check_password(serializer.validated_data.get('old_password')):
                return Response(
                    {'old_password': ['Wrong password.']},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Set new password
            user.set_password(serializer.validated_data.get('new_password'))
            user.save()
            
            # Generate new tokens after password change (for security)
            refresh = RefreshToken.for_user(user)
            access_token = str(refresh.access_token)
            refresh_token = str(refresh)
            
            response = Response(
                {'message': 'Password updated successfully. Please login again.'},
                status=status.HTTP_200_OK
            )
            
            # ✅ Update cookies with new tokens
            set_auth_cookies(response, access_token, refresh_token)
            
            print(f"✅ Password changed for: {user.email}")
            return response
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class UserListView(generics.ListAPIView):
    """
    API endpoint for listing users (admin only)
    Works with httpOnly cookies through middleware
    """
    queryset = User.objects.all()
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAdminUser]


# OPTIONAL: GET CURRENT USER
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_current_user(request):
    """
    Get current authenticated user
    Useful for frontend to check auth status
    """
    serializer = UserSerializer(request.user)
    return Response(serializer.data)












# from rest_framework import generics, status, permissions
# from rest_framework.response import Response
# from rest_framework.views import APIView
# from django.contrib.auth import get_user_model
# from .serializers import (
#     UserRegistrationSerializer,
#     UserSerializer,
#     ChangePasswordSerializer
# )

# User = get_user_model()

# class RegisterView(generics.CreateAPIView):

#     """
#     API endpoint for user registration
#     """
#     queryset = User.objects.all()
#     permission_classes = [permissions.AllowAny]
#     serializer_class = UserRegistrationSerializer
    
#     def create(self, request, *args, **kwargs):
#         serializer = self.get_serializer(data=request.data)  # This is same as:serializer = UserRegistrationSerializer(data=request.data)
#         serializer.is_valid(raise_exception=True)
#         user = serializer.save()
        
#         return Response({
#             'user': UserSerializer(user).data,
#             'message': 'User registered successfully. Please log in.'
#         }, status=status.HTTP_201_CREATED)

# class UserProfileView(generics.RetrieveUpdateAPIView):
#     """
#     API endpoint for viewing and updating user profile
#     """
#     serializer_class = UserSerializer
#     permission_classes = [permissions.IsAuthenticated]
    
#     def get_object(self):
#         return self.request.user

# class ChangePasswordView(APIView):
#     """
#     API endpoint for changing password
#     """
#     permission_classes = [permissions.IsAuthenticated]
    
#     def post(self, request):
#         serializer = ChangePasswordSerializer(data=request.data)
        
#         if serializer.is_valid():
#             user = request.user
            
#             # Check old password
#             if not user.check_password(serializer.validated_data.get('old_password')):
#                 return Response(
#                     {'old_password': ['Wrong password.']},
#                     status=status.HTTP_400_BAD_REQUEST
#                 )     
#             # Set new password
#             user.set_password(serializer.validated_data.get('new_password'))
#             user.save()
            
#             return Response(
#                 {'message': 'Password updated successfully.'},
#                 status=status.HTTP_200_OK
#             )
        
#         return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

# class UserListView(generics.ListAPIView):
#     """
#     API endpoint for listing users (admin only)
#     """
#     queryset = User.objects.all()
#     serializer_class = UserSerializer
#     permission_classes = [permissions.IsAdminUser]
