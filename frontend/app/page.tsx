'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';
import api from '@/lib/api';
import { useAuthStore } from '@/lib/store';

export default function Home() {
  const router = useRouter();
  const setUser = useAuthStore((state) => state.setUser);

  useEffect(() => {
    const checkAuth = async () => {
      try {
        // 🔐 Browser automatically sends httpOnly cookie
        const response = await api.get('/auth/profile/');
        setUser(response.data);
        router.push('/dashboard');
      } catch (error) {
        router.push('/login');
      }
    };

    checkAuth();
  }, [setUser,router]); /* You are not changing the router object.You are calling a method on the router.*/

  return (
    <div className="min-h-screen flex items-center justify-center">
      <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-maroon-700"></div>
    </div>
  );
}