from django.contrib.auth.models import AbstractUser
from django.db import models

class User(AbstractUser):
    """Custom User model with email as username field"""
    
    email = models.EmailField(
        unique=True,
        verbose_name='Email Address',
        help_text='Required. Enter a valid email address.'
    )
    phone_number = models.CharField(
        max_length=15,
        blank=True,
        null=True,
        verbose_name='Phone Number'
    )
    profile_picture = models.ImageField(
        upload_to='profiles/',
        blank=True,
        null=True,
        verbose_name='Profile Picture'
    )
    is_email_verified = models.BooleanField(
        default=False,
        verbose_name='Email Verified'
    )
    created_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name='Created At'
    )
    updated_at = models.DateTimeField(
        auto_now=True,
        verbose_name='Updated At'
    )
    
    # Use email for authentication instead of username
    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']
    
    class Meta:
        db_table = 'users'
        verbose_name = 'User'
        verbose_name_plural = 'Users'
        ordering = ['-created_at']
    
    def __str__(self):
        return self.email
    
    def get_full_name(self):
        """Return the full name or email if names not available"""
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.email
