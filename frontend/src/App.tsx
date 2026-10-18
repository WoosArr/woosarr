import { useEffect, useState } from "react";
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { useAuth } from "./auth";
import { Icon, type IconName } from "./icons";
import { useTheme } from "./theme";
import { ErrorBanner, Spinner } from "./ui";
import { LoginScreen, SetupScreen } from "./pages/AuthScreens";
import DashboardPage from "./pages/Dashboard";
import UsersPage from "./pages/Users";
import RolesPage from "./pages/Roles";
import LibrariesPage from "./pages/Libraries";
import ReactionsPage from "./pages/Reactions";
import RequestsPage from "./pages/Requests";
import LogsPage from "./pages/Logs";
import SettingsPage from "./pages/Settings";
import SetupPage from "./pages/Setup";

const NAV: { to: string; label: string; icon: IconName }[] = [
  { to: "/", label: "Dashboard", icon: "dashboard" },
  { to: "/users", label: "Users", icon: "users" },
  { to: "/roles", label: "Roles", icon: "roles" },
  { to: "/libraries", label: "Libraries", icon: "library" },
  { to: "/reactions", label: "Reactions", icon: "check" },
  { to: "/requests", label: "Requests", icon: "download" },
  { to: "/logs", label: "Logs", icon: "logs" },
  { to: "/settings", label: "Settings", icon: "settings" },
  { to: "/setup", label: "Setup", icon: "setup" },
];

function ThemeToggle() {
  const { resolved, setPrefs } = useTheme();
  const next = resolved === "dark" ? "light" : "dark";
  return (
    <button className="btn sm" onClick={() => setPrefs({ mode: next })} aria-label={`Switch to ${next} mode`}>
      <Icon name={resolved === "dark" ? "sun" : "moon"} size={15} /> {next === "dark" ? "Dark" : "Light"} mode
    </button>
  );
}

function Shell() {
  const { status, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const location = useLocation();
  useEffect(() => setOpen(false), [location.pathname]);

  return (
    <div className="shell">
      <a className="skip-link" href="#main">Skip to content</a>
      <header className="topbar">
        <button className="btn ghost icon-btn" onClick={() => setOpen(true)} aria-label="Open navigation" aria-expanded={open}>
          <Icon name="menu" />
        </button>
        <strong>WooSarr</strong>
      </header>
      {open ? <div className="scrim" onClick={() => setOpen(false)} aria-hidden="true" /> : null}
      <nav className={`sidebar${open ? " open" : ""}`} aria-label="Main">
        <div className="brand">
          <img src="/logo-64.webp" alt="" width={30} height={30} />
          WooSarr
        </div>
        {NAV.map((n) => (
          <NavLink key={n.to} to={n.to} end={n.to === "/"} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`}>
            <Icon name={n.icon} /> {n.label}
          </NavLink>
        ))}
        <div className="sidebar-foot">
          <ThemeToggle />
          <div className="row spread">
            <span className="row">
              <Icon name="user" size={15} /> {status?.user?.username}
            </span>
            <button className="btn ghost sm" onClick={() => void logout()}>
              <Icon name="logout" size={15} /> Sign out
            </button>
          </div>
        </div>
      </nav>
      <main className="main" id="main" tabIndex={-1}>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/users" element={<UsersPage />} />
          <Route path="/roles" element={<RolesPage />} />
          <Route path="/libraries" element={<LibrariesPage />} />
          <Route path="/reactions" element={<ReactionsPage />} />
          <Route path="/requests" element={<RequestsPage />} />
          <Route path="/logs" element={<LogsPage />} />
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/setup" element={<SetupPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  );
}

export default function App() {
  const { status, error } = useAuth();
  if (!status) {
    return (
      <main className="auth-wrap">
        {error ? <ErrorBanner error={error} /> : <Spinner label="Connecting to WooSarr" />}
      </main>
    );
  }
  if (status.setup_required) return <SetupScreen />;
  if (!status.authenticated) return <LoginScreen />;
  return (
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  );
}
