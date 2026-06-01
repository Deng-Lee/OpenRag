import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import { useEffect, useState } from 'react';
import Login from './pages/Login';
import Register from './pages/Register';
import Files from './pages/Files';
import Tasks from './pages/Tasks';
import Permissions from './pages/Permissions';
import AdminPermissions from './pages/AdminPermissions';
import ServiceTokens from './pages/ServiceTokens';
import Settings from './pages/Settings';
import SearchPage from './pages/Search';
import DocumentChunks from './pages/DocumentChunks';
import { authAPI } from './services/api';

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const isAuthenticated = !!localStorage.getItem('token');
  return isAuthenticated ? <>{children}</> : <Navigate to="/login" />;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);

  useEffect(() => {
    let active = true;
    authAPI
      .getCurrentUser()
      .then((user) => {
        if (active) setIsAdmin(user.is_admin === true);
      })
      .catch(() => {
        if (active) setIsAdmin(false);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  if (loading) return null;
  return isAdmin ? <>{children}</> : <Navigate to="/files" replace />;
}

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/register" element={<Register />} />
        <Route
          path="/files"
          element={
            <ProtectedRoute>
              <Files />
            </ProtectedRoute>
          }
        />
        <Route
          path="/search"
          element={
            <ProtectedRoute>
              <SearchPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/workspaces/:workspaceId/files/:fileId/chunks"
          element={
            <ProtectedRoute>
              <DocumentChunks />
            </ProtectedRoute>
          }
        />
        <Route
          path="/permissions"
          element={
            <ProtectedRoute>
              <Permissions />
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin-permissions"
          element={
            <ProtectedRoute>
              <AdminRoute>
                <AdminPermissions />
              </AdminRoute>
            </ProtectedRoute>
          }
        />
        <Route
          path="/service-tokens"
          element={
            <ProtectedRoute>
              <AdminRoute>
                <ServiceTokens />
              </AdminRoute>
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings"
          element={
            <ProtectedRoute>
              <Settings />
            </ProtectedRoute>
          }
        />
        <Route
          path="/tasks"
          element={
            <ProtectedRoute>
              <Tasks />
            </ProtectedRoute>
          }
        />
        <Route path="/" element={<Navigate to="/files" />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
