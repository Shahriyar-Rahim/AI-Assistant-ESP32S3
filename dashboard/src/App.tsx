import { FormEvent, ReactNode, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  AlarmClock,
  Bell,
  CalendarDays,
  Cloud,
  Code2,
  Cpu,
  Gamepad2,
  Gauge,
  KeyRound,
  Monitor,
  PlugZap,
  Radio,
  Server,
  Settings,
  Shield,
  Smile,
  Upload,
  Wifi,
  WifiOff,
  Workflow,
} from "lucide-react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

type Device = {
  device_id: string;
  name: string;
  connected: boolean;
  last_seen: string | null;
  simulated: boolean;
  config: Record<string, unknown>;
};
type EventItem = {
  id: string;
  title: string;
  kind: string;
  description?: string;
  start_at: string;
  end_at: string | null;
  timezone_name: string;
  recurrence_rule?: string | null;
  completed: boolean;
};
type GameSession = {
  session_id: string;
  game: string;
  version: number;
  status: string;
  state: Record<string, unknown>;
};
type NotificationItem = {
  id: string;
  source_type: string;
  title: string;
  payload: Record<string, unknown>;
  delivered: boolean;
  created_at: string;
};
type IntegrationAccount = {
  provider: "github" | "gmail" | "linkedin";
  configured: boolean;
  credential_source: "environment" | "encrypted_storage" | null;
  scopes: string[];
  connected_at: string | null;
};
const configuredApi = (import.meta.env.VITE_API_URL ?? "").trim();
const apiBase = (
  configuredApi && !configuredApi.includes(",")
    ? configuredApi
    : `${window.location.protocol}//${window.location.hostname}:8000`
).replace(/\/$/, "");
const tokenKey = "assistant-access-token";

async function fetchJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(tokenKey);
  const isFormData =
    typeof FormData !== "undefined" && init.body instanceof FormData;
  const response = await fetch(`${apiBase}${path}`, {
    ...init,
    headers: {
      ...(init.body && !isFormData
        ? { "Content-Type": "application/json" }
        : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new Error(
      body?.detail?.message ??
        body?.detail ??
        `Backend returned HTTP ${response.status}`,
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function Login({ onLogin }: { onLogin: () => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setBusy(true);
    const form = new FormData(event.currentTarget);
    try {
      const result = await fetchJson<{ access_token: string }>(
        "/api/v1/auth/login",
        {
          method: "POST",
          body: JSON.stringify({
            username: form.get("username"),
            password: form.get("password"),
          }),
        },
      );
      localStorage.setItem(tokenKey, result.access_token);
      onLogin();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Sign-in failed");
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="login-shell">
      <div className="login-card">
        <div className="brand-icon">
          <Shield size={22} />
        </div>
        <p className="eyebrow">SECURE DEVELOPER ACCESS</p>
        <h1>Sign in</h1>
        <p className="subheading">
          Use the administrator credentials configured on the backend.
        </p>
        <form onSubmit={submit}>
          <label>
            Username
            <input name="username" autoComplete="username" required />
          </label>
          <label>
            Password
            <input
              name="password"
              type="password"
              autoComplete="current-password"
              required
            />
          </label>
          {error && (
            <div className="alert error" role="alert">
              {error}
            </div>
          )}
          <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </div>
    </main>
  );
}

function Shell({
  children,
  onLogout,
}: {
  children: ReactNode;
  onLogout: () => void;
}) {
  const links = [
    { to: "/", text: "Overview", icon: <Activity size={16} /> },
    { to: "/devices", text: "Devices", icon: <Cpu size={16} /> },
    { to: "/monitoring", text: "Live telemetry", icon: <Gauge size={16} /> },
    { to: "/display", text: "Display studio", icon: <Smile size={16} /> },
    { to: "/ota", text: "Firmware OTA", icon: <Upload size={16} /> },
    { to: "/calendar", text: "Calendar", icon: <CalendarDays size={16} /> },
    { to: "/alarms", text: "Alarms", icon: <AlarmClock size={16} /> },
    { to: "/games", text: "Mini-games", icon: <Gamepad2 size={16} /> },
    { to: "/vision", text: "Camera", icon: <Shield size={16} /> },
    { to: "/game-history", text: "Game history", icon: <Gauge size={16} /> },
    { to: "/brain", text: "Local brain", icon: <Cpu size={16} /> },
    { to: "/ai", text: "AI provider", icon: <Activity size={16} /> },
    { to: "/voice", text: "Voice activity", icon: <Radio size={16} /> },
    {
      to: "/personalization",
      text: "Personalization",
      icon: <Activity size={16} />,
    },
    { to: "/biometric", text: "Voice security", icon: <Shield size={16} /> },
    { to: "/integrations", text: "Integrations", icon: <PlugZap size={16} /> },
    { to: "/credentials", text: "Credentials", icon: <KeyRound size={16} /> },
    { to: "/notifications", text: "Notifications", icon: <Bell size={16} /> },
    { to: "/settings", text: "Settings", icon: <Settings size={16} /> },
    { to: "/developer", text: "Developer tools", icon: <Code2 size={16} /> },
    { to: "/weather", text: "Weather", icon: <Cloud size={16} /> },
    { to: "/automation", text: "Automation", icon: <Workflow size={16} /> },
    { to: "/audit", text: "Audit logs", icon: <Shield size={16} /> },
  ];
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <Cpu size={21} />
          </div>
          <div>
            <strong>EDGE / ASSISTANT</strong>
            <span>DEVELOPER CONTROL</span>
          </div>
        </div>
        <nav>
          {links.map((link) => (
            <Link key={link.to} to={link.to}>
              {link.icon}
              {link.text}
            </Link>
          ))}
        </nav>
        <button className="logout" onClick={onLogout}>
          Sign out
        </button>
      </aside>
      <main className="content">
        <header className="topbar">
          <span className="system-state">
            <i className="status-dot online" />
            Authenticated dashboard
          </span>
          <button
            className="theme-toggle"
            onClick={() => document.documentElement.classList.toggle("light")}
          >
            Toggle theme
          </button>
        </header>
        {children}
      </main>
    </div>
  );
}

function Overview() {
  const health = useQuery({
    queryKey: ["health"],
    queryFn: () => fetchJson<{ status: string }>("/api/v1/health"),
    refetchInterval: 10_000,
  });
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
    refetchInterval: 2_000,
  });
  return (
    <>
      <section className="page-heading">
        <div>
          <p className="eyebrow">SYSTEM OVERVIEW</p>
          <h1>Device control</h1>
          <p className="subheading">
            Live connectivity from the assistant backend.
          </p>
        </div>
        <span className="protocol-chip">
          <Activity size={15} /> API v1
        </span>
      </section>
      {health.isError && (
        <div className="alert error">
          <WifiOff size={17} />
          Backend is unavailable.
        </div>
      )}
      {devices.isError && (
        <div className="alert error">
          <WifiOff size={17} />
          Could not load active device sessions.
        </div>
      )}
      <section className="metrics">
        <article className="metric-card">
          <div className="metric-icon purple">
            <Server size={18} />
          </div>
          <span>API STATUS</span>
          <strong>
            {health.data?.status === "ok"
              ? "Healthy"
              : health.isLoading
                ? "Checking"
                : "Unavailable"}
          </strong>
          <small>GET /api/v1/health</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon green">
            <Radio size={18} />
          </div>
          <span>CONNECTED DEVICES</span>
          <strong>
            {devices.data?.devices.length ?? (devices.isLoading ? "…" : "—")}
          </strong>
          <small>Active WebSocket sessions</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon blue">
            <Wifi size={18} />
          </div>
          <span>PROTOCOL</span>
          <strong>Version 1</strong>
          <small>Authenticated device channel</small>
        </article>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Connected devices</h2>
            <p>Live authenticated WebSocket sessions.</p>
          </div>
          <span className="live-tag">
            <i /> LIVE
          </span>
        </div>
        {devices.isLoading ? (
          <div className="empty-state">Loading device connections…</div>
        ) : devices.data?.devices.length ? (
          <div className="device-list">
            {devices.data.devices.map((device) => (
              <article className="device-row" key={device.device_id}>
                <div className="device-avatar">
                  <Cpu size={20} />
                </div>
                <div className="device-info">
                  <strong>{device.device_id}</strong>
                  <span>
                    Last packet{" "}
                    {device.last_seen
                      ? new Date(device.last_seen).toLocaleTimeString()
                      : "No audio seen"}
                  </span>
                </div>
                {device.simulated && (
                  <span className="simulated-tag">SIMULATED</span>
                )}
                <span className="connection">
                  <i />
                  {device.connected ? "Connected" : "Disconnected"}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <div className="empty-state">
            <Radio size={24} />
            <strong>No active device connection</strong>
            <span>Simulator sessions are explicitly marked.</span>
          </div>
        )}
      </section>
      <footer>
        Voice audio retention is encrypted and opt-in. Simulator traffic is not
        physical hardware validation.
      </footer>
    </>
  );
}

function CalendarPage() {
  const query = useQuery({
    queryKey: ["events"],
    queryFn: () =>
      fetchJson<{ items: EventItem[]; total: number }>(
        "/api/v1/calendar/events?limit=100",
      ),
  });
  const [error, setError] = useState("");
  const [editing, setEditing] = useState<EventItem | null>(null);
  async function createEvent(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const payload = {
        title: data.get("title"),
        kind: data.get("kind"),
        start_at: new Date(String(data.get("start_at"))).toISOString(),
        timezone_name:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        recurrence_rule: data.get("recurrence_rule") || null,
      };
      await fetchJson(
        editing
          ? `/api/v1/calendar/events/${editing.id}`
          : "/api/v1/calendar/events",
        {
          method: editing ? "PATCH" : "POST",
          body: JSON.stringify(payload),
        },
      );
      await query.refetch();
      form.reset();
      setEditing(null);
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not create event",
      );
    }
  }
  async function removeEvent(id: string) {
    try {
      await fetchJson(`/api/v1/calendar/events/${id}`, { method: "DELETE" });
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not delete event",
      );
    }
  }
  async function exportCsv() {
    try {
      const response = await fetch(`${apiBase}/api/v1/calendar/export.csv`, {
        headers: { Authorization: `Bearer ${localStorage.getItem(tokenKey)}` },
      });
      if (!response.ok)
        throw new Error(`Export failed with HTTP ${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = "calendar.csv";
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Export failed");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="SCHEDULE"
        title="Calendar"
        description="Events are stored as UTC instants with IANA timezone and recurrence rules."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>{editing ? "Edit event" : "Create event"}</h2>
            <p>
              {editing
                ? "Update the selected event."
                : "Add an event to your schedule."}
            </p>
          </div>
          <button className="secondary" onClick={exportCsv}>
            Export CSV
          </button>
        </div>
        <form
          key={editing?.id ?? "new"}
          className="form-grid"
          onSubmit={createEvent}
        >
          <label>
            Title
            <input
              name="title"
              required
              maxLength={200}
              defaultValue={editing?.title}
            />
          </label>
          <label>
            Type
            <select name="kind" defaultValue={editing?.kind ?? "event"}>
              <option value="event">Event</option>
              <option value="meeting">Meeting</option>
              <option value="birthday">Birthday</option>
              <option value="function">Special function</option>
              <option value="alarm">Alarm</option>
            </select>
          </label>
          <label>
            Starts at
            <input
              name="start_at"
              type="datetime-local"
              required
              defaultValue={
                editing
                  ? new Date(
                      new Date(editing.start_at).getTime() -
                        new Date().getTimezoneOffset() * 60000,
                    )
                      .toISOString()
                      .slice(0, 16)
                  : undefined
              }
            />
          </label>
          <label>
            Recurrence (RRULE)
            <input
              name="recurrence_rule"
              placeholder="FREQ=WEEKLY;COUNT=4"
              defaultValue={editing?.recurrence_rule ?? ""}
            />
          </label>
          <button>{editing ? "Save changes" : "Create event"}</button>
          {editing && (
            <button
              className="secondary"
              type="button"
              onClick={() => setEditing(null)}
            >
              Cancel edit
            </button>
          )}
        </form>
        {error && <div className="alert error">{error}</div>}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Upcoming events</h2>
            <p>{query.data?.total ?? 0} scheduled</p>
          </div>
        </div>
        {query.isError && (
          <div className="alert error">{query.error.message}</div>
        )}
        {query.data?.items.map((item) => (
          <article className="event-row" key={item.id}>
            <div>
              <strong>{item.title}</strong>
              <span>
                {item.kind} · {new Date(item.start_at).toLocaleString()} (
                {item.timezone_name})
              </span>
            </div>
            <div className="button-row">
              <button className="secondary" onClick={() => setEditing(item)}>
                Edit
              </button>
              <button className="danger" onClick={() => removeEvent(item.id)}>
                Delete
              </button>
            </div>
          </article>
        ))}
      </section>
      <ReminderPanel onError={setError} />
      <TodoPanel onError={setError} />
    </>
  );
}

function AlarmsPage() {
  const query = useQuery({
    queryKey: ["events", "alarms"],
    queryFn: () =>
      fetchJson<{ items: EventItem[]; total: number }>(
        "/api/v1/calendar/events?kind=alarm&limit=100",
      ),
  });
  const [editing, setEditing] = useState<EventItem | null>(null);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  async function saveAlarm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    try {
      const payload = {
        title: values.get("title"),
        kind: "alarm",
        start_at: new Date(String(values.get("start_at"))).toISOString(),
        timezone_name:
          Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        recurrence_rule: values.get("recurrence_rule") || null,
      };
      await fetchJson(
        editing
          ? `/api/v1/calendar/events/${editing.id}`
          : "/api/v1/calendar/events",
        {
          method: editing ? "PATCH" : "POST",
          body: JSON.stringify(payload),
        },
      );
      await query.refetch();
      form.reset();
      setEditing(null);
      setError("");
      setMessage(editing ? "Alarm updated." : "Alarm scheduled.");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not save alarm",
      );
    }
  }

  async function deleteAlarm(id: string) {
    try {
      await fetchJson(`/api/v1/calendar/events/${id}`, { method: "DELETE" });
      await query.refetch();
      setMessage("Alarm deleted.");
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not delete alarm",
      );
    }
  }

  return (
    <>
      <PageHeading
        eyebrow="SCHEDULE"
        title="Alarms"
        description="Create one-time or recurring alarms. Due alarms use the same persistent scheduler and device-notification delivery path as calendar reminders."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>{editing ? "Edit alarm" : "Schedule an alarm"}</h2>
            <p>
              Times are stored as UTC instants with your current IANA timezone.
            </p>
          </div>
        </div>
        <form
          key={editing?.id ?? "new-alarm"}
          className="form-grid"
          onSubmit={saveAlarm}
        >
          <label>
            Alarm name
            <input
              name="title"
              required
              maxLength={200}
              defaultValue={editing?.title}
            />
          </label>
          <label>
            Time
            <input
              name="start_at"
              type="datetime-local"
              required
              defaultValue={
                editing
                  ? new Date(
                      new Date(editing.start_at).getTime() -
                        new Date().getTimezoneOffset() * 60000,
                    )
                      .toISOString()
                      .slice(0, 16)
                  : undefined
              }
            />
          </label>
          <label>
            Recurrence (RRULE)
            <input
              name="recurrence_rule"
              placeholder="FREQ=DAILY;COUNT=7"
              defaultValue={editing?.recurrence_rule ?? ""}
            />
          </label>
          <button>
            <AlarmClock size={16} />
            {editing ? "Save alarm" : "Create alarm"}
          </button>
          {editing && (
            <button
              className="secondary"
              type="button"
              onClick={() => setEditing(null)}
            >
              Cancel edit
            </button>
          )}
        </form>
        {message && (
          <div className="response-box" role="status">
            {message}
          </div>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Scheduled alarms</h2>
            <p>{query.data?.total ?? 0} alarms</p>
          </div>
        </div>
        {query.isLoading && <div className="empty-state">Loading alarms…</div>}
        {query.isError && (
          <div className="alert error" role="alert">
            {query.error.message}
          </div>
        )}
        {!query.isLoading &&
          !query.isError &&
          query.data?.items.length === 0 && (
            <div className="empty-state">
              <AlarmClock size={22} />
              No alarms scheduled.
            </div>
          )}
        {query.data?.items.map((alarm) => (
          <article className="event-row" key={alarm.id}>
            <div>
              <strong>{alarm.title}</strong>
              <span>
                {new Date(alarm.start_at).toLocaleString()} ·{" "}
                {alarm.timezone_name}
                {alarm.recurrence_rule ? ` · ${alarm.recurrence_rule}` : ""}
              </span>
            </div>
            <div className="button-row">
              <button className="secondary" onClick={() => setEditing(alarm)}>
                Edit
              </button>
              <button
                className="danger"
                onClick={() => void deleteAlarm(alarm.id)}
              >
                Delete
              </button>
            </div>
          </article>
        ))}
      </section>
    </>
  );
}

function SettingsPage() {
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
  });
  const [selected, setSelected] = useState("");
  const [brightness, setBrightness] = useState(50);
  const [volume, setVolume] = useState(100);
  const [microphoneEnabled, setMicrophoneEnabled] = useState(true);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const physicalDevices =
    devices.data?.devices.filter((device) => !device.simulated) ?? [];
  const device = physicalDevices.find((item) => item.device_id === selected);

  function chooseDevice(deviceId: string) {
    setSelected(deviceId);
    const selectedDevice = physicalDevices.find(
      (item) => item.device_id === deviceId,
    );
    setBrightness(Number(selectedDevice?.config.brightness ?? 50));
    setVolume(Number(selectedDevice?.config.volume ?? 100));
    setMicrophoneEnabled(selectedDevice?.config.microphone_enabled !== false);
  }

  async function saveSettings() {
    if (!selected) return;
    try {
      await fetchJson(
        `/api/v1/devices/${encodeURIComponent(selected)}/config`,
        {
          method: "PATCH",
          body: JSON.stringify({
            brightness,
            volume,
            microphone_enabled: microphoneEnabled,
          }),
        },
      );
      await devices.refetch();
      setMessage("Device settings saved.");
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not save device settings",
      );
    }
  }

  return (
    <>
      <PageHeading
        eyebrow="PREFERENCES"
        title="Device settings"
        description="Manage persisted per-device brightness and microphone uplink controls. Changes are applied live when connected and included in the next device status sync."
      />
      <section className="panel">
        <div className="form-grid">
          <label>
            Physical device
            <select
              value={selected}
              onChange={(event) => chooseDevice(event.target.value)}
            >
              <option value="">Select a registered device</option>
              {physicalDevices.map((item) => (
                <option key={item.device_id} value={item.device_id}>
                  {item.name} · {item.device_id}
                </option>
              ))}
            </select>
          </label>
          <label>
            Display brightness: {brightness}%
            <input
              type="range"
              min="0"
              max="100"
              value={brightness}
              disabled={!device}
              onChange={(event) => setBrightness(Number(event.target.value))}
            />
          </label>
          <label>
            Speaker volume: {volume}%
            <input
              type="range"
              min="0"
              max="100"
              value={volume}
              disabled={!device}
              onChange={(event) => setVolume(Number(event.target.value))}
            />
          </label>
          <label>
            Microphone uplink
            <select
              value={microphoneEnabled ? "enabled" : "disabled"}
              disabled={!device}
              onChange={(event) =>
                setMicrophoneEnabled(event.target.value === "enabled")
              }
            >
              <option value="enabled">Enabled</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
          <button disabled={!device} onClick={() => void saveSettings()}>
            <Settings size={16} />
            Save device settings
          </button>
        </div>
        {device && (
          <p>
            {device.connected
              ? "Connected; controls are sent immediately."
              : "Offline; settings will apply on the next connection."}
          </p>
        )}
        {devices.isLoading && (
          <div className="empty-state">Loading device settings…</div>
        )}
        {devices.isError && (
          <div className="alert error" role="alert">
            {devices.error.message}
          </div>
        )}
        {message && (
          <div className="response-box" role="status">
            {message}
          </div>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </section>
      <section className="panel">
        <h2>Backend connection</h2>
        <p>
          Dashboard API origin: <code>{apiBase}</code>
        </p>
        <p>
          Device tokens and provider secrets are configured on the backend and
          are never stored in dashboard preferences.
        </p>
      </section>
    </>
  );
}

function DevicesPage() {
  const query = useQuery({
    queryKey: ["devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
  });
  const [selected, setSelected] = useState("");
  const [brightness, setBrightness] = useState(50);
  const [microphoneEnabled, setMicrophoneEnabled] = useState(true);
  const [issuedToken, setIssuedToken] = useState("");
  const [error, setError] = useState("");
  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    try {
      const device = await fetchJson<{
        device_id: string;
        device_token: string;
      }>("/api/v1/devices/register", {
        method: "POST",
        body: JSON.stringify({
          device_id: values.get("device_id"),
          name: values.get("name"),
        }),
      });
      setIssuedToken(`${device.device_id}: ${device.device_token}`);
      form.reset();
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Device registration failed",
      );
    }
  }
  async function updateConfig(values: Record<string, unknown>) {
    if (!selected) return;
    try {
      await fetchJson(
        `/api/v1/devices/${encodeURIComponent(selected)}/config`,
        { method: "PATCH", body: JSON.stringify(values) },
      );
      setError("");
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Device command failed",
      );
    }
  }
  const chosen = query.data?.devices.find(
    (device) => device.device_id === selected,
  );
  return (
    <>
      <PageHeading
        eyebrow="HARDWARE"
        title="Device management"
        description="Enroll devices and send validated controls to connected firmware."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Register device</h2>
            <p>
              Device credentials are displayed only once. Store them securely on
              the device.
            </p>
          </div>
        </div>
        <form className="form-grid" onSubmit={register}>
          <label>
            Device ID
            <input
              name="device_id"
              required
              pattern="[a-zA-Z0-9._-]+"
              maxLength={128}
            />
          </label>
          <label>
            Display name
            <input name="name" required maxLength={120} />
          </label>
          <button>Register device</button>
        </form>
        {issuedToken && (
          <div className="credential-box">
            <strong>Copy this device token now</strong>
            <code>{issuedToken}</code>
            <button
              className="secondary"
              onClick={() => navigator.clipboard.writeText(issuedToken)}
            >
              Copy token
            </button>
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Registered devices</h2>
            <p>{query.data?.devices.length ?? 0} device credentials</p>
          </div>
        </div>
        {query.isError && (
          <div className="alert error">{query.error.message}</div>
        )}
        {query.data?.devices.map((device) => (
          <article className="device-row" key={device.device_id}>
            <div className="device-avatar">
              <Cpu size={20} />
            </div>
            <div className="device-info">
              <strong>{device.name}</strong>
              <span>
                {device.device_id} ·{" "}
                {device.last_seen
                  ? new Date(device.last_seen).toLocaleString()
                  : "Never connected"}
              </span>
            </div>
            {device.simulated && (
              <span className="simulated-tag">SIMULATED</span>
            )}
            <span className="connection">
              <i
                style={{ background: device.connected ? undefined : "#687089" }}
              />
              {device.connected ? "Connected" : "Offline"}
            </span>
            <button
              className="secondary"
              onClick={() => {
                setSelected(device.device_id);
                setBrightness(Number(device.config.brightness ?? 50));
                setMicrophoneEnabled(
                  device.config.microphone_enabled !== false,
                );
              }}
            >
              Control
            </button>
          </article>
        ))}
      </section>
      {chosen && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>Controls · {chosen.name}</h2>
              <p>
                Offline changes are stored and included in the next device
                hello.
              </p>
            </div>
          </div>
          <label className="range-control">
            Brightness: {brightness}%
            <input
              type="range"
              min="0"
              max="100"
              value={brightness}
              onChange={(event) => setBrightness(Number(event.target.value))}
            />
          </label>
          <div className="button-row">
            <button onClick={() => updateConfig({ brightness })}>
              Apply brightness
            </button>
            <button
              className="secondary"
              onClick={() => {
                const next = !microphoneEnabled;
                setMicrophoneEnabled(next);
                updateConfig({ microphone_enabled: next });
              }}
            >
              {microphoneEnabled ? "Disable microphone" : "Enable microphone"}
            </button>
          </div>
          {error && <div className="alert error">{error}</div>}
        </section>
      )}
    </>
  );
}

function ReminderPanel({ onError }: { onError: (message: string) => void }) {
  const query = useQuery({
    queryKey: ["reminders"],
    queryFn: () =>
      fetchJson<{
        items: {
          id: string;
          title: string;
          due_at: string;
          completed: boolean;
        }[];
      }>("/api/v1/reminders"),
  });
  async function addReminder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await fetchJson("/api/v1/reminders", {
        method: "POST",
        body: JSON.stringify({
          title: data.get("title"),
          due_at: new Date(String(data.get("due_at"))).toISOString(),
          timezone_name:
            Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        }),
      });
      form.reset();
      await query.refetch();
    } catch (error) {
      onError(
        error instanceof Error ? error.message : "Could not add reminder",
      );
    }
  }
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Reminders</h2>
          <p>Mark due items complete from the schedule.</p>
        </div>
      </div>
      <form className="form-grid" onSubmit={addReminder}>
        <label>
          Reminder
          <input name="title" required />
        </label>
        <label>
          Due at
          <input name="due_at" type="datetime-local" required />
        </label>
        <button>Add reminder</button>
      </form>
      {query.data?.items.map((item) => (
        <div className="event-row" key={item.id}>
          <div>
            <strong>{item.title}</strong>
            <span>{new Date(item.due_at).toLocaleString()}</span>
          </div>
          <button
            className="secondary"
            disabled={item.completed}
            onClick={async () => {
              await fetchJson(`/api/v1/reminders/${item.id}`, {
                method: "PATCH",
                body: JSON.stringify({ completed: true }),
              });
              await query.refetch();
            }}
          >
            {item.completed ? "Completed" : "Complete"}
          </button>
        </div>
      ))}
    </section>
  );
}

function TodoPanel({ onError }: { onError: (message: string) => void }) {
  const query = useQuery({
    queryKey: ["todos"],
    queryFn: () =>
      fetchJson<{
        items: {
          id: string;
          title: string;
          due_at: string | null;
          completed: boolean;
          list_title: string | null;
        }[];
      }>("/api/v1/todos"),
  });
  const lists = useQuery({
    queryKey: ["todo-lists"],
    queryFn: () =>
      fetchJson<{ items: { id: string; title: string }[] }>(
        "/api/v1/todo-lists",
      ),
  });
  async function addTodo(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const due = String(data.get("due_at") || "");
    try {
      const listId = String(data.get("list_id") || "");
      await fetchJson(
        listId
          ? `/api/v1/todo-lists/${encodeURIComponent(listId)}/items`
          : "/api/v1/todos",
        {
          method: "POST",
          body: JSON.stringify({
            title: data.get("title"),
            due_at: due ? new Date(due).toISOString() : null,
          }),
        },
      );
      form.reset();
      await query.refetch();
    } catch (reason) {
      onError(reason instanceof Error ? reason.message : "Could not add todo");
    }
  }
  async function completeTodo(id: string) {
    try {
      await fetchJson(`/api/v1/todos/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ completed: true }),
      });
      await query.refetch();
    } catch (reason) {
      onError(
        reason instanceof Error ? reason.message : "Could not update todo",
      );
    }
  }
  async function addList(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await fetchJson("/api/v1/todo-lists", {
        method: "POST",
        body: JSON.stringify({ title: new FormData(form).get("title") }),
      });
      form.reset();
      await lists.refetch();
    } catch (reason) {
      onError(
        reason instanceof Error ? reason.message : "Could not create list",
      );
    }
  }
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>To-dos</h2>
          <p>
            {query.data?.items.filter((item) => !item.completed).length ?? 0}{" "}
            pending · mirrored on the device home screen
          </p>
        </div>
      </div>
      <form className="form-grid" onSubmit={addList}>
        <label>
          New list
          <input name="title" required maxLength={200} />
        </label>
        <button>Create list</button>
      </form>
      <form className="form-grid" onSubmit={addTodo}>
        <label>
          Task
          <input name="title" required maxLength={200} />
        </label>
        <label>
          List
          <select name="list_id">
            <option value="">Standalone task</option>
            {lists.data?.items.map((list) => (
              <option key={list.id} value={list.id}>
                {list.title}
              </option>
            ))}
          </select>
        </label>
        <label>
          Optional due date
          <input name="due_at" type="datetime-local" />
        </label>
        <button>Add to-do</button>
      </form>
      {lists.isError && (
        <div className="alert error">{lists.error.message}</div>
      )}
      {query.isError && (
        <div className="alert error">{query.error.message}</div>
      )}
      {query.data?.items.map((item) => (
        <div className="event-row" key={item.id}>
          <div>
            <strong>{item.title}</strong>
            <span>
              {item.list_title ? `${item.list_title} · ` : ""}
              {item.due_at
                ? new Date(item.due_at).toLocaleString()
                : "No due date"}
            </span>
          </div>
          <button
            className="secondary"
            disabled={item.completed}
            onClick={() => completeTodo(item.id)}
          >
            {item.completed ? "Completed" : "Complete"}
          </button>
        </div>
      ))}
    </section>
  );
}

function GamesPage() {
  const [session, setSession] = useState<GameSession | null>(null);
  const [error, setError] = useState("");
  async function start(game: string) {
    try {
      const result = await fetchJson<GameSession>(
        `/api/v1/games/${game}/sessions`,
        { method: "POST", body: JSON.stringify({}) },
      );
      setSession(result);
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not start game",
      );
    }
  }
  async function move(moveBody: Record<string, unknown>) {
    if (!session) return;
    try {
      const updated = await fetchJson<GameSession>(
        `/api/v1/games/sessions/${session.session_id}/moves`,
        {
          method: "POST",
          body: JSON.stringify({
            expected_version: session.version,
            move: moveBody,
          }),
        },
      );
      setSession(updated);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Move failed");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="PLAY"
        title="Mini-games"
        description="Game rules and state are authoritative on the backend."
      />
      <section className="game-picker">
        {[
          ["tic_tac_toe", "Tic-Tac-Toe"],
          ["rock_paper_scissors", "Rock, Paper, Scissors"],
          ["number_guessing", "Number Guessing"],
          ["trivia", "Trivia"],
          ["hangman", "Hangman"],
          ["word_association", "Word Association"],
        ].map(([id, name]) => (
          <button key={id} className="game-card" onClick={() => start(id)}>
            <Gamepad2 size={18} />
            <strong>{name}</strong>
            <span>Start session</span>
          </button>
        ))}
      </section>
      {error && <div className="alert error">{error}</div>}
      {session && (
        <section className="panel">
          <div className="panel-heading">
            <div>
              <h2>{session.game.replaceAll("_", " ")}</h2>
              <p>
                Session {session.session_id} · version {session.version} ·{" "}
                {session.status}
              </p>
            </div>
          </div>
          <GameControls session={session} onMove={move} />
        </section>
      )}
    </>
  );
}

type GameResultRow = {
  id: string;
  game: string;
  outcome: string;
  score: number;
  max_score: number;
  rounds: number;
  duration_seconds: number;
  difficulty: string;
  device_id: string | null;
  simulated: boolean;
  finished_at: string;
};
type GameStats = {
  total_games: number;
  total_wins: number;
  summary: string;
  games: Record<
    string,
    {
      played: number;
      wins: number;
      losses: number;
      draws: number;
      abandoned: number;
      win_rate: number | null;
      average_duration_seconds: number;
      best_score: number;
      streak: { kind: string | null; length: number };
      recent_form: string[];
      last_played: string;
    }
  >;
  patterns: {
    favorite: string | null;
    strongest: string | null;
    weakest: string | null;
    play_hours: number[];
    suggested_difficulty: Record<string, string>;
    insights: string[];
  };
};
const gameLabel = (game: string) =>
  game.replace(/_/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());

function GameHistoryPage() {
  const [error, setError] = useState("");
  const stats = useQuery({
    queryKey: ["game-stats"],
    queryFn: () => fetchJson<GameStats>("/api/v1/games/stats"),
    refetchInterval: 5_000,
  });
  const history = useQuery({
    queryKey: ["game-history"],
    queryFn: () =>
      fetchJson<{ results: GameResultRow[] }>(
        "/api/v1/games/history?limit=100",
      ),
    refetchInterval: 5_000,
  });
  async function clear() {
    if (
      !window.confirm(
        "Delete all saved game history and learned play patterns?",
      )
    )
      return;
    try {
      await fetchJson<void>("/api/v1/games/history", { method: "DELETE" });
      setError("");
      await Promise.all([stats.refetch(), history.refetch()]);
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not clear history",
      );
    }
  }
  const data = stats.data;
  const games = Object.entries(data?.games ?? {});
  return (
    <>
      <PageHeading
        eyebrow="PLAY"
        title="Game history"
        description="Results of games played on the ESP32-S3. The assistant uses these patterns to adapt difficulty, recommend games, and answer questions about your play."
      />
      {(stats.isError || history.isError) && (
        <div className="alert error">Could not load game data.</div>
      )}
      {error && <div className="alert error">{error}</div>}
      <section className="metrics">
        <article className="metric-card">
          <div className="metric-icon purple">
            <Gamepad2 size={18} />
          </div>
          <span>GAMES PLAYED</span>
          <strong>{data?.total_games ?? "—"}</strong>
          <small>All saved results</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon green">
            <Gauge size={18} />
          </div>
          <span>WINS</span>
          <strong>{data?.total_wins ?? "—"}</strong>
          <small>
            {data && data.total_games
              ? `${Math.round((data.total_wins / data.total_games) * 100)}% of games`
              : "No games yet"}
          </small>
        </article>
        <article className="metric-card">
          <div className="metric-icon blue">
            <Smile size={18} />
          </div>
          <span>FAVORITE GAME</span>
          <strong>
            {data?.patterns.favorite ? gameLabel(data.patterns.favorite) : "—"}
          </strong>
          <small>
            {data?.patterns.play_hours.length
              ? `Usually played around ${data.patterns.play_hours.map((hour) => `${hour}:00`).join(", ")} UTC`
              : "Learns from your play"}
          </small>
        </article>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Learned patterns</h2>
            <p>{data?.summary ?? "Loading…"}</p>
          </div>
          <button className="secondary" onClick={clear}>
            Clear history
          </button>
        </div>
        {data?.patterns.insights.length ? (
          <ul>
            {data.patterns.insights.map((insight) => (
              <li key={insight}>{insight}</li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            Play a game on the device to start learning your patterns.
          </div>
        )}
        {data && Object.keys(data.patterns.suggested_difficulty).length > 0 && (
          <p>
            Adaptive difficulty:{" "}
            {Object.entries(data.patterns.suggested_difficulty)
              .map(([game, level]) => `${gameLabel(game)} → ${level}`)
              .join(" · ")}
          </p>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Per-game statistics</h2>
            <p>Win rate, streaks and recent form.</p>
          </div>
        </div>
        {games.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>Game</th>
                <th>Played</th>
                <th>W / L / D</th>
                <th>Win rate</th>
                <th>Best score</th>
                <th>Avg time</th>
                <th>Streak</th>
                <th>Recent</th>
              </tr>
            </thead>
            <tbody>
              {games.map(([game, item]) => (
                <tr key={game}>
                  <td>{gameLabel(game)}</td>
                  <td>{item.played}</td>
                  <td>
                    {item.wins} / {item.losses} / {item.draws}
                  </td>
                  <td>
                    {item.win_rate === null
                      ? "—"
                      : `${Math.round(item.win_rate * 100)}%`}
                  </td>
                  <td>{item.best_score}</td>
                  <td>{item.average_duration_seconds}s</td>
                  <td>
                    {item.streak.kind
                      ? `${item.streak.length} ${item.streak.kind}`
                      : "—"}
                  </td>
                  <td>{item.recent_form.join(" ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty-state">No finished games yet.</div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>History</h2>
            <p>Most recent 100 results.</p>
          </div>
        </div>
        {history.data?.results.length ? (
          <table className="data-table">
            <thead>
              <tr>
                <th>When</th>
                <th>Game</th>
                <th>Outcome</th>
                <th>Score</th>
                <th>Rounds</th>
                <th>Duration</th>
                <th>Difficulty</th>
                <th>Device</th>
              </tr>
            </thead>
            <tbody>
              {history.data.results.map((row) => (
                <tr key={row.id}>
                  <td>{new Date(row.finished_at).toLocaleString()}</td>
                  <td>{gameLabel(row.game)}</td>
                  <td>{row.outcome}</td>
                  <td>
                    {row.max_score
                      ? `${row.score}/${row.max_score}`
                      : row.score}
                  </td>
                  <td>{row.rounds}</td>
                  <td>{row.duration_seconds}s</td>
                  <td>{row.difficulty}</td>
                  <td>
                    {row.device_id ?? "—"}
                    {row.simulated ? " (SIMULATED)" : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <div className="empty-state">No history saved yet.</div>
        )}
      </section>
    </>
  );
}

function BrainPage() {
  const [error, setError] = useState("");
  type BrainRow = {
    id: string;
    kind: string;
    prompt: string;
    uses: number;
    source: string;
    answer: string;
  };
  const brain = useQuery({
    queryKey: ["brain"],
    queryFn: () =>
      fetchJson<{
        entries: number;
        kinds: Record<string, number>;
        total_local_answers: number;
        local_first: boolean;
        runs_on: string;
        facts: { id: string; text: string }[];
        top: BrainRow[];
        recent: BrainRow[];
      }>("/api/v1/brain"),
    refetchInterval: 5_000,
  });
  async function remove(path: string, confirmText?: string) {
    if (confirmText && !window.confirm(confirmText)) return;
    try {
      await fetchJson<unknown>(path, { method: "DELETE" });
      setError("");
      await brain.refetch();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not delete");
    }
  }
  const data = brain.data;
  const rows = (items: BrainRow[]) => (
    <table className="data-table">
      <thead>
        <tr>
          <th>You said</th>
          <th>Learned answer</th>
          <th>Kind</th>
          <th>Used</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {items.map((row) => (
          <tr key={row.id}>
            <td>{row.prompt}</td>
            <td>{row.answer}</td>
            <td>
              {row.kind} · {row.source}
            </td>
            <td>{row.uses}</td>
            <td>
              <button
                className="secondary"
                onClick={() => remove(`/api/v1/brain/${row.id}`)}
              >
                Forget
              </button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
  return (
    <>
      <PageHeading
        eyebrow="LEARN"
        title="Local brain"
        description="A small retrieval brain that learns from you and from cloud AI answers, so common questions are answered without the cloud. It runs on the backend host, not on the ESP32-S3."
      />
      {brain.isError && (
        <div className="alert error">Could not load the local brain.</div>
      )}
      {error && <div className="alert error">{error}</div>}
      <section className="metrics">
        <article className="metric-card">
          <div className="metric-icon purple">
            <Cpu size={18} />
          </div>
          <span>LEARNED ENTRIES</span>
          <strong>{data?.entries ?? "—"}</strong>
          <small>
            {Object.entries(data?.kinds ?? {})
              .map(([kind, count]) => `${kind}: ${count}`)
              .join(" · ") || "Nothing learned yet"}
          </small>
        </article>
        <article className="metric-card">
          <div className="metric-icon green">
            <Gauge size={18} />
          </div>
          <span>ANSWERED LOCALLY</span>
          <strong>{data?.total_local_answers ?? "—"}</strong>
          <small>
            {data?.local_first ? "Local-first enabled" : "Cloud-first"}
          </small>
        </article>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>What I know about you</h2>
            <p>Say “my name is…” or “remember that…” to the assistant.</p>
          </div>
          <button
            className="secondary"
            onClick={() =>
              remove(
                "/api/v1/brain",
                "Delete everything the local brain has learned?",
              )
            }
          >
            Forget everything
          </button>
        </div>
        {data?.facts.length ? (
          <ul>
            {data.facts.map((fact) => (
              <li key={fact.id}>
                {fact.text}{" "}
                <button
                  className="secondary"
                  onClick={() => remove(`/api/v1/brain/${fact.id}`)}
                >
                  Forget
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">No personal facts saved yet.</div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Most used answers</h2>
          </div>
        </div>
        {data?.top.length ? (
          rows(data.top)
        ) : (
          <div className="empty-state">
            Ask the assistant questions; cloud answers are learned and reused.
          </div>
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Recently learned</h2>
          </div>
        </div>
        {data?.recent.length ? (
          rows(data.recent)
        ) : (
          <div className="empty-state">Nothing yet.</div>
        )}
      </section>
    </>
  );
}

function GameControls({
  session,
  onMove,
}: {
  session: GameSession;
  onMove: (move: Record<string, unknown>) => void;
}) {
  const state = session.state;
  if (session.game === "tic_tac_toe") {
    const board = state.board as string[];
    return (
      <div className="ttt-board">
        {board.map((cell, index) => (
          <button
            key={index}
            disabled={!!cell || session.status !== "active"}
            onClick={() => onMove({ position: index })}
          >
            {cell}
          </button>
        ))}
      </div>
    );
  }
  if (session.game === "rock_paper_scissors")
    return (
      <div className="button-row">
        {["rock", "paper", "scissors"].map((choice) => (
          <button key={choice} onClick={() => onMove({ choice })}>
            {choice}
          </button>
        ))}
      </div>
    );
  if (session.game === "number_guessing")
    return (
      <MoveForm
        key={session.version}
        name="guess"
        label="Guess 1–100"
        type="number"
        onSubmit={(value) => onMove({ guess: Number(value) })}
      >
        <p>
          Guesses: {String(state.guesses)} · Hint:{" "}
          {String(state.last_hint ?? "—")}
        </p>
      </MoveForm>
    );
  if (session.game === "trivia") {
    const questions = state.questions as {
      question: string;
      choices: string[];
    }[];
    const index = Number(state.question_index);
    const question = questions[index];
    if (!question)
      return (
        <p>
          Trivia complete. Score: {String(state.score)} · streak:{" "}
          {String(state.streak)}
        </p>
      );
    return (
      <div>
        <h3>{question.question}</h3>
        <div className="button-row">
          {question.choices.map((choice) => (
            <button
              key={choice[0]}
              onClick={() => onMove({ answer: choice[0] })}
            >
              {choice}
            </button>
          ))}
        </div>
        <p>
          Score: {String(state.score)} · Streak: {String(state.streak)}
        </p>
      </div>
    );
  }
  if (session.game === "hangman")
    return (
      <MoveForm
        key={session.version}
        name="letter"
        label={`Guess a letter · ${String(state.display ?? "______")}`}
        onSubmit={(value) => onMove({ letter: value })}
      >
        <p>
          Mistakes: {String(state.mistakes)} / {String(state.max_mistakes)}
        </p>
      </MoveForm>
    );
  return (
    <MoveForm
      key={session.version}
      name="word"
      label={`Associate with “${String(state.target)}”`}
      onSubmit={(value) => onMove({ word: value })}
    >
      <p>Turns: {String(state.turns)} / 5</p>
    </MoveForm>
  );
}

function MoveForm({
  name,
  label,
  type = "text",
  onSubmit,
  children,
}: {
  name: string;
  label: string;
  type?: string;
  onSubmit: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <>
      <form
        className="form-grid"
        onSubmit={(event) => {
          event.preventDefault();
          const form = event.currentTarget;
          onSubmit(String(new FormData(form).get(name)));
          form.reset();
        }}
      >
        <label>
          {label}
          <input
            name={name}
            type={type}
            required
            min={type === "number" ? 1 : undefined}
            max={type === "number" ? 100 : undefined}
          />
        </label>
        <button>Submit move</button>
      </form>
      {children}
    </>
  );
}

function AIPage() {
  const providers = useQuery({
    queryKey: ["ai-providers"],
    queryFn: () =>
      fetchJson<{
        providers: {
          provider: string;
          configured: boolean;
          active: boolean;
          model: string | null;
        }[];
      }>("/api/v1/ai/providers"),
  });
  const [provider, setProvider] = useState("ollama");
  const [model, setModel] = useState("");
  const [systemPrompt, setSystemPrompt] = useState(
    "You are a helpful voice assistant.",
  );
  const [message, setMessage] = useState("");
  const [conversationId, setConversationId] = useState("");
  const [chatResponse, setChatResponse] = useState("");
  const [error, setError] = useState("");
  async function saveProvider(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      await fetchJson(`/api/v1/ai/providers/${provider}`, {
        method: "PUT",
        body: JSON.stringify({ model, system_prompt: systemPrompt }),
      });
      await providers.refetch();
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not configure provider",
      );
    }
  }
  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    try {
      const result = await fetchJson<{
        conversation_id: string;
        provider: string;
        model: string;
        response: string;
      }>("/api/v1/ai/chat", {
        method: "POST",
        body: JSON.stringify({
          message,
          conversation_id: conversationId || null,
        }),
      });
      setConversationId(result.conversation_id);
      setChatResponse(
        `${result.provider} / ${result.model}: ${result.response}`,
      );
      setMessage("");
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "AI request failed");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="AI SERVICES"
        title="Provider settings"
        description="Provider requests run on the server. API keys are never sent to the browser."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Available providers</h2>
            <p>Keys are provided only through backend environment settings.</p>
          </div>
        </div>
        {providers.isError && (
          <div className="alert error">{providers.error.message}</div>
        )}
        {providers.data?.providers.map((item) => (
          <div className="event-row" key={item.provider}>
            <div>
              <strong>{item.provider}</strong>
              <span>
                {item.configured
                  ? "Credentials/endpoint configured"
                  : "Missing credentials"}
                {item.active ? ` · active model ${item.model}` : ""}
              </span>
            </div>
            <button
              className="secondary"
              disabled={!item.configured && item.provider !== "ollama"}
              onClick={() => setProvider(item.provider)}
            >
              Select
            </button>
          </div>
        ))}
      </section>
      <section className="panel">
        <h2>Configure and chat</h2>
        <form className="form-grid" onSubmit={saveProvider}>
          <label>
            Provider
            <select
              value={provider}
              onChange={(event) => setProvider(event.target.value)}
            >
              <option value="openai">OpenAI</option>
              <option value="gemini">Google Gemini</option>
              <option value="ollama">Ollama</option>
            </select>
          </label>
          <label>
            Model
            <input
              value={model}
              onChange={(event) => setModel(event.target.value)}
              required
              maxLength={120}
            />
          </label>
          <label>
            System prompt
            <input
              value={systemPrompt}
              onChange={(event) => setSystemPrompt(event.target.value)}
              maxLength={4000}
            />
          </label>
          <button>Activate provider</button>
        </form>
        <form className="form-grid" onSubmit={sendMessage}>
          <label>
            Message
            <input
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              required
              maxLength={8000}
            />
          </label>
          <button>Send to AI</button>
        </form>
        {error && <div className="alert error">{error}</div>}
        {chatResponse && <div className="response-box">{chatResponse}</div>}
      </section>
    </>
  );
}

function VoicePage() {
  const [transcript, setTranscript] = useState("");
  const [error, setError] = useState("");
  async function transcribe(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      const result = await fetchJson<{ text: string }>(
        "/api/v1/audio/transcriptions?language=" + data.get("language"),
        { method: "POST", body: data },
      );
      setTranscript(result.text);
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Transcription failed",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="AUDIO SERVICES"
        title="Voice activity"
        description="Uploaded WAV recordings are processed in memory only. Device-command retention is separately opt-in under Personalization."
      />
      <section className="panel">
        <form className="form-grid" onSubmit={transcribe}>
          <label>
            WAV recording
            <input type="file" name="file" accept="audio/wav,.wav" required />
          </label>
          <label>
            Language
            <select name="language">
              <option value="en">English</option>
              <option value="bn">Bengali</option>
            </select>
          </label>
          <button>Transcribe</button>
        </form>
        {error && <div className="alert error">{error}</div>}
        {transcript && <div className="response-box">{transcript}</div>}
      </section>
    </>
  );
}

function PersonalizationPage() {
  const query = useQuery({
    queryKey: ["personalization"],
    queryFn: () =>
      fetchJson<{
        enabled: boolean;
        interaction_count: number;
        stored_audio_count: number;
        audio_retention_days: number;
        encryption_configured: boolean;
      }>("/api/v1/assistant/personalization"),
  });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function setEnabled(enabled: boolean) {
    try {
      await fetchJson("/api/v1/assistant/personalization", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      });
      setMessage(
        enabled
          ? "Personalized learning enabled."
          : "Personalized learning disabled.",
      );
      setError("");
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not update personalization",
      );
    }
  }
  async function clearHistory() {
    try {
      await fetchJson("/api/v1/assistant/personalization/history", {
        method: "DELETE",
      });
      setMessage("Encrypted voice samples and learned style profile deleted.");
      setError("");
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not delete personalization history",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="VOICE PREFERENCES"
        title="Personalization"
        description="Learn response length and preferred language from verified voice interactions. Raw PCM and transcript are encrypted and retained only while enabled."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Learned profile</h2>
            <p>
              {query.data?.enabled
                ? "Learning is enabled"
                : "Learning is disabled"}{" "}
              · {query.data?.interaction_count ?? 0} interactions learned ·{" "}
              {query.data?.stored_audio_count ?? 0} encrypted audio samples
            </p>
          </div>
          <span className="protocol-chip">
            {query.data?.audio_retention_days ?? 30}-day retention
          </span>
        </div>
        <p>
          Audio is encrypted with VOICE_ENCRYPTION_KEY, is not used to train a
          shared model, and is automatically deleted after the retention period.
          Disabling stops new retention; use delete to erase saved samples and
          the learned profile.
        </p>
        {!query.data?.encryption_configured && (
          <div className="alert error">
            Set VOICE_ENCRYPTION_KEY on the backend before enabling encrypted
            voice retention.
          </div>
        )}
        {error && <div className="alert error">{error}</div>}
        {message && <div className="response-box">{message}</div>}
        <div className="button-row">
          <button
            disabled={query.data?.enabled || !query.data?.encryption_configured}
            onClick={() => setEnabled(true)}
          >
            Enable learning
          </button>
          <button
            className="secondary"
            disabled={!query.data?.enabled}
            onClick={() => setEnabled(false)}
          >
            Disable learning
          </button>
          <button className="secondary" onClick={clearHistory}>
            Delete learned history
          </button>
        </div>
      </section>
    </>
  );
}

function BiometricPage() {
  const query = useQuery({
    queryKey: ["biometric-status"],
    queryFn: () =>
      fetchJson<{
        enrolled: boolean;
        threshold: number;
        anti_spoofing: string;
      }>("/api/v1/biometric/status"),
  });
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  async function submit(
    event: FormEvent<HTMLFormElement>,
    mode: "enroll" | "verify",
  ) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const response = await fetchJson<{
        enrolled?: boolean;
        verified?: boolean;
        similarity?: number;
        threshold?: number;
      }>(`/api/v1/biometric/${mode}`, { method: "POST", body: data });
      setResult(
        mode === "enroll"
          ? "Master voice enrollment saved (audio discarded)."
          : `Verified: ${response.verified}; similarity ${response.similarity}; threshold ${response.threshold}`,
      );
      setError("");
      await query.refetch();
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : `Voice ${mode} failed`,
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="IDENTITY"
        title="Voice security"
        description="Embeddings are encrypted; voice similarity is not liveness or replay protection."
      />
      <section className="panel">
        <p>
          Enrollment: {query.data?.enrolled ? "Configured" : "Not enrolled"} ·
          Threshold: {query.data?.threshold ?? "—"} · Anti-spoofing:{" "}
          {query.data?.anti_spoofing ?? "—"}
        </p>
        <p>
          Use a clear 3–20 second PCM WAV sample. Audio files are not retained.
        </p>
        <form
          className="form-grid"
          onSubmit={(event) => submit(event, "enroll")}
        >
          <label>
            Enrollment recording
            <input type="file" name="file" accept="audio/wav,.wav" required />
          </label>
          <button>Enroll / replace</button>
        </form>
        <form
          className="form-grid"
          onSubmit={(event) => submit(event, "verify")}
        >
          <label>
            Verification recording
            <input type="file" name="file" accept="audio/wav,.wav" required />
          </label>
          <button>Verify voice</button>
        </form>
        {error && <div className="alert error">{error}</div>}
        {result && <div className="response-box">{result}</div>}
      </section>
    </>
  );
}

function AuditPage() {
  const [filter, setFilter] = useState("");
  const query = useQuery({
    queryKey: ["audit", filter],
    queryFn: () =>
      fetchJson<{
        items: {
          id: string;
          timestamp: string;
          event_type: string;
          action: string;
          result: string;
        }[];
        total: number;
      }>(
        `/api/v1/audit?limit=100${filter ? `&event_type=${encodeURIComponent(filter)}` : ""}`,
      ),
  });
  const [error, setError] = useState("");
  async function download(format: "csv" | "json") {
    try {
      const response = await fetch(`${apiBase}/api/v1/audit/export.${format}`, {
        headers: { Authorization: `Bearer ${localStorage.getItem(tokenKey)}` },
      });
      if (!response.ok)
        throw new Error(`Download failed with HTTP ${response.status}`);
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `audit.${format}`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Download failed");
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="SYSTEM HISTORY"
        title="Audit logs"
        description="Server-side activity records with structured metadata."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Activity</h2>
            <p>{query.data?.total ?? 0} matching records</p>
          </div>
          <div className="button-row">
            <button className="secondary" onClick={() => download("csv")}>
              Download CSV
            </button>
            <button className="secondary" onClick={() => download("json")}>
              Download JSON
            </button>
          </div>
        </div>
        <label className="filter-label">
          Event type
          <input
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            placeholder="Filter exact event type"
          />
        </label>
        {error && <div className="alert error">{error}</div>}
        {query.isError && (
          <div className="alert error">{query.error.message}</div>
        )}
        {query.data?.items.map((item) => (
          <article className="event-row" key={item.id}>
            <div>
              <strong>{item.action}</strong>
              <span>
                {item.event_type} · {new Date(item.timestamp).toLocaleString()}
              </span>
            </div>
            <span className="connection">{item.result}</span>
          </article>
        ))}
      </section>
    </>
  );
}

type Endpoint = {
  id: string;
  name: string;
  url: string;
  interval_seconds: number;
  enabled: boolean;
  last_status: string | null;
  last_response_ms: number | null;
  failure_count: number;
};

function AutomationPage() {
  const query = useQuery({
    queryKey: ["automation-endpoints"],
    queryFn: () =>
      fetchJson<{ endpoints: Endpoint[] }>("/api/v1/automation/endpoints"),
    refetchInterval: 30_000,
  });
  const [selected, setSelected] = useState<Endpoint | null>(null);
  const [history, setHistory] = useState<
    {
      checked_at: string;
      http_status: number | null;
      response_ms: number | null;
      result: string;
      error: string | null;
    }[]
  >([]);
  const [error, setError] = useState("");
  async function addEndpoint(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    try {
      await fetchJson("/api/v1/automation/endpoints", {
        method: "POST",
        body: JSON.stringify({
          name: data.get("name"),
          url: data.get("url"),
          interval_seconds: Number(data.get("interval_seconds")),
          enabled: true,
        }),
      });
      form.reset();
      await query.refetch();
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Could not add endpoint",
      );
    }
  }
  async function ping(endpoint: Endpoint) {
    try {
      await fetchJson(`/api/v1/automation/endpoints/${endpoint.id}/ping`, {
        method: "POST",
      });
      const result = await fetchJson<{ history: typeof history }>(
        `/api/v1/automation/endpoints/${endpoint.id}/history`,
      );
      setSelected(endpoint);
      setHistory(result.history);
      await query.refetch();
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Endpoint check failed",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="AUTOMATION"
        title="Endpoint monitoring"
        description="Scheduled HTTPS health checks run on the backend; private and reserved destinations are rejected."
      />
      <section className="panel">
        <h2>Monitor a public endpoint</h2>
        <form className="form-grid" onSubmit={addEndpoint}>
          <label>
            Name
            <input name="name" required maxLength={120} />
          </label>
          <label>
            HTTPS URL
            <input
              name="url"
              type="url"
              required
              placeholder="https://service.example/health"
            />
          </label>
          <label>
            Interval (seconds)
            <input
              name="interval_seconds"
              type="number"
              min="60"
              max="86400"
              defaultValue="600"
              required
            />
          </label>
          <button>Add endpoint</button>
        </form>
        {error && <div className="alert error">{error}</div>}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Configured checks</h2>
            <p>
              {query.data?.endpoints.length ?? 0} endpoints · default cadence 10
              minutes
            </p>
          </div>
        </div>
        {query.isError && (
          <div className="alert error">{query.error.message}</div>
        )}
        {query.data?.endpoints.map((endpoint) => (
          <article className="event-row" key={endpoint.id}>
            <div>
              <strong>{endpoint.name}</strong>
              <span>
                {endpoint.url} · {endpoint.last_status ?? "not checked"} ·{" "}
                {endpoint.last_response_ms ?? "—"} ms · failures{" "}
                {endpoint.failure_count}
              </span>
            </div>
            <div className="button-row">
              <button className="secondary" onClick={() => ping(endpoint)}>
                Check now
              </button>
              <button
                className="secondary"
                onClick={async () => {
                  try {
                    await fetchJson(
                      `/api/v1/automation/endpoints/${endpoint.id}`,
                      { method: "DELETE" },
                    );
                    await query.refetch();
                  } catch (reason) {
                    setError(
                      reason instanceof Error
                        ? reason.message
                        : "Delete failed",
                    );
                  }
                }}
              >
                Remove
              </button>
            </div>
          </article>
        ))}
      </section>
      {selected && (
        <section className="panel">
          <h2>Check history · {selected.name}</h2>
          {history.map((item, index) => (
            <article className="event-row" key={`${item.checked_at}-${index}`}>
              <div>
                <strong>
                  {item.result} · HTTP {item.http_status ?? "—"}
                </strong>
                <span>
                  {new Date(item.checked_at).toLocaleString()} ·{" "}
                  {item.response_ms ?? "—"} ms{" "}
                  {item.error ? `· ${item.error}` : ""}
                </span>
              </div>
            </article>
          ))}
        </section>
      )}
    </>
  );
}

function IntegrationsPage() {
  const repositories = useQuery({
    queryKey: ["github-repositories"],
    queryFn: () =>
      fetchJson<{
        repositories: {
          name: string;
          private: boolean;
          stars: number;
          forks: number;
          updated_at: string;
          url: string;
        }[];
      }>("/api/v1/integrations/github/repositories"),
    retry: false,
  });
  const notifications = useQuery({
    queryKey: ["github-notifications"],
    queryFn: () =>
      fetchJson<{
        notifications: {
          id: string;
          subject: string;
          repository: string;
          reason: string;
          updated_at: string;
        }[];
      }>("/api/v1/integrations/github/notifications"),
    retry: false,
  });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function saveGitHub(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      await fetchJson("/api/v1/integrations/github/credentials", {
        method: "PUT",
        body: JSON.stringify({ token: new FormData(form).get("token") }),
      });
      form.reset();
      await Promise.all([repositories.refetch(), notifications.refetch()]);
      setMessage(
        "GitHub credential encrypted at rest; secret is not displayed again.",
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not store GitHub credential",
      );
    }
  }
  async function beginOAuth(provider: "gmail" | "linkedin") {
    try {
      const result = await fetchJson<{ authorization_url: string }>(
        `/api/v1/integrations/${provider}/authorize`,
      );
      window.location.assign(result.authorization_url);
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : `Could not authorize ${provider}`,
      );
    }
  }
  const gmail = useQuery({
    queryKey: ["gmail-unread"],
    queryFn: () =>
      fetchJson<{
        unread: {
          id: string;
          from: string;
          subject: string;
          date: string;
          snippet: string;
        }[];
      }>("/api/v1/integrations/gmail/unread"),
    enabled: false,
    retry: false,
  });
  const linkedin = useQuery({
    queryKey: ["linkedin-profile"],
    queryFn: () =>
      fetchJson<{
        profile: { name: string; email?: string; picture?: string };
      }>("/api/v1/integrations/linkedin/profile"),
    enabled: false,
    retry: false,
  });
  return (
    <>
      <PageHeading
        eyebrow="CONNECTED SERVICES"
        title="Integrations"
        description="Official APIs only. Credentials are stored server-side with encryption; unsupported provider features are not scraped."
      />
      <section className="panel">
        <h2>GitHub</h2>
        <form className="form-grid" onSubmit={saveGitHub}>
          <label>
            Personal access token
            <input
              name="token"
              type="password"
              autoComplete="new-password"
              minLength={10}
              maxLength={500}
              required
            />
          </label>
          <button>Store encrypted credential</button>
        </form>
        {message && <div className="response-box">{message}</div>}
        {repositories.isError && (
          <div className="alert error">{repositories.error.message}</div>
        )}
        {repositories.data?.repositories.map((repo) => (
          <article className="event-row" key={repo.name}>
            <div>
              <strong>
                <a href={repo.url} target="_blank" rel="noreferrer">
                  {repo.name}
                </a>
              </strong>
              <span>
                {repo.private ? "Private" : "Public"} · ★ {repo.stars} · forks{" "}
                {repo.forks} · updated{" "}
                {new Date(repo.updated_at).toLocaleDateString()}
              </span>
            </div>
          </article>
        ))}
        {notifications.data?.notifications.map((item) => (
          <article className="event-row" key={item.id}>
            <div>
              <strong>{item.subject}</strong>
              <span>
                {item.repository} · {item.reason}
              </span>
            </div>
          </article>
        ))}
      </section>
      <section className="panel">
        <h2>Gmail</h2>
        <p>
          Google OAuth and Gmail API scopes are required. The API provides
          unread-message summaries only.
        </p>
        <div className="button-row">
          <button onClick={() => beginOAuth("gmail")}>Authorize Gmail</button>
          <button className="secondary" onClick={() => void gmail.refetch()}>
            Load unread
          </button>
        </div>
        {gmail.isError && (
          <div className="alert error">{gmail.error.message}</div>
        )}
        {gmail.data?.unread.map((item) => (
          <article className="event-row" key={item.id}>
            <div>
              <strong>{item.subject || "(no subject)"}</strong>
              <span>
                {item.from} · {new Date(item.date).toLocaleString()} ·{" "}
                {item.snippet}
              </span>
            </div>
          </article>
        ))}
      </section>
      <section className="panel">
        <h2>LinkedIn</h2>
        <p>
          Supported integration is limited to the authorized OpenID profile
          endpoint. Private messages and analytics are not exposed by this API
          client.
        </p>
        <div className="button-row">
          <button onClick={() => beginOAuth("linkedin")}>
            Authorize LinkedIn
          </button>
          <button className="secondary" onClick={() => void linkedin.refetch()}>
            Load profile
          </button>
        </div>
        {linkedin.isError && (
          <div className="alert error">{linkedin.error.message}</div>
        )}
        {linkedin.data && (
          <div className="response-box">
            {linkedin.data.profile.name} ·{" "}
            {linkedin.data.profile.email ?? "email not granted"}
          </div>
        )}
      </section>
      {error && <div className="alert error">{error}</div>}
    </>
  );
}

function NotificationsPage() {
  const query = useQuery({
    queryKey: ["notifications"],
    queryFn: () =>
      fetchJson<{ items: NotificationItem[]; total: number }>(
        "/api/v1/notifications?limit=100",
      ),
    refetchInterval: 30_000,
  });
  const [filter, setFilter] = useState<"all" | "pending" | "delivered">("all");
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function setDelivered(item: NotificationItem, delivered: boolean) {
    try {
      await fetchJson(`/api/v1/notifications/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({ delivered }),
      });
      await query.refetch();
      setMessage(
        delivered
          ? "Notification marked as delivered."
          : "Notification marked as pending.",
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not update notification",
      );
    }
  }
  const items = (query.data?.items ?? []).filter(
    (item) =>
      filter === "all" ||
      (filter === "delivered" ? item.delivered : !item.delivered),
  );
  return (
    <>
      <PageHeading
        eyebrow="ALERTS"
        title="Notification center"
        description="Review persisted calendar/reminder alerts and track their device-delivery state."
      />
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Notifications</h2>
            <p>
              {query.data?.total ?? 0} records ·{" "}
              {items.filter((item) => !item.delivered).length} pending in this
              view
            </p>
          </div>
          <label>
            Show
            <select
              value={filter}
              onChange={(event) =>
                setFilter(event.target.value as typeof filter)
              }
            >
              <option value="all">All</option>
              <option value="pending">Pending</option>
              <option value="delivered">Delivered</option>
            </select>
          </label>
        </div>
        <p>
          Changing this marker does not send or retry a device notification.
        </p>
        {message && (
          <div className="response-box" role="status">
            {message}
          </div>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
        {query.isLoading && (
          <div className="empty-state">Loading notifications…</div>
        )}
        {query.isError && (
          <div className="alert error">{query.error.message}</div>
        )}
        {!query.isLoading && !query.isError && items.length === 0 && (
          <div className="empty-state">
            <Bell size={22} />
            No notifications match this filter.
          </div>
        )}
        {items.map((item) => (
          <article className="event-row" key={item.id}>
            <div>
              <strong>{item.title}</strong>
              <span>
                {item.source_type} ·{" "}
                {new Date(item.created_at).toLocaleString()} ·{" "}
                {item.delivered ? "Delivered to device" : "Pending delivery"}
              </span>
            </div>
            <button
              className={item.delivered ? "secondary" : ""}
              onClick={() => void setDelivered(item, !item.delivered)}
            >
              {item.delivered ? "Mark pending" : "Mark delivered"}
            </button>
          </article>
        ))}
      </section>
    </>
  );
}

function CredentialsPage() {
  const query = useQuery({
    queryKey: ["integration-accounts"],
    queryFn: () =>
      fetchJson<{ accounts: IntegrationAccount[] }>(
        "/api/v1/integrations/accounts",
      ),
  });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function disconnect(account: IntegrationAccount) {
    if (account.credential_source === "environment") return;
    if (
      !window.confirm(
        `Remove the assistant's stored ${account.provider} credential? This does not revoke it at the provider.`,
      )
    )
      return;
    try {
      await fetchJson(`/api/v1/integrations/${account.provider}/credentials`, {
        method: "DELETE",
      });
      await query.refetch();
      setMessage(
        `${account.provider} credential removed from assistant storage. Revoke the grant/token at the provider separately.`,
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : `Could not remove ${account.provider} credentials`,
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="ACCESS CONTROL"
        title="Credential hub"
        description="Configuration metadata only; stored secrets remain encrypted on the backend and are never returned to the browser. Provider connectivity is not live-verified here."
      />
      {message && (
        <div className="response-box" role="status">
          {message}
        </div>
      )}
      {error && (
        <div className="alert error" role="alert">
          {error}
        </div>
      )}
      {query.isError && (
        <div className="alert error" role="alert">
          {query.error.message}
        </div>
      )}
      <section className="panel">
        <h2>Connected providers</h2>
        <p>
          Removing a credential here deletes the assistant's stored copy.
          Provider-side tokens or consent grants must be revoked separately.
        </p>
        {query.isLoading && (
          <div className="empty-state">Loading credential status…</div>
        )}
        {query.data?.accounts.map((account) => (
          <article className="event-row" key={account.provider}>
            <div>
              <strong>{account.provider}</strong>
              <span>
                {account.configured
                  ? `Credential configured · ${account.credential_source === "environment" ? "backend environment secret" : "encrypted storage"}`
                  : "No credential stored"}
                {account.connected_at
                  ? ` · stored ${new Date(account.connected_at).toLocaleString()}`
                  : ""}
                {account.scopes.length
                  ? ` · scopes: ${account.scopes.join(", ")}`
                  : ""}
              </span>
            </div>
            {account.configured && (
              <button
                className="danger"
                disabled={account.credential_source === "environment"}
                onClick={() => void disconnect(account)}
              >
                {account.credential_source === "environment"
                  ? "Managed by backend"
                  : "Remove stored credential"}
              </button>
            )}
          </article>
        ))}
      </section>
    </>
  );
}

function DeveloperPage() {
  const health = useQuery({
    queryKey: ["developer-health"],
    queryFn: () => fetchJson<{ status: string }>("/api/v1/health"),
    refetchInterval: 10_000,
  });
  const devices = useQuery({
    queryKey: ["developer-devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
    refetchInterval: 5_000,
  });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [deviceId, setDeviceId] = useState("simulated-buddy");
  const [simulatorToken, setSimulatorToken] = useState("");
  const [scenario, setScenario] = useState("observe");
  const shellQuote = (value: string) => `'${value.replaceAll("'", "'\\''")}'`;
  const simulatorCommand = simulatorToken
    ? `env BACKEND_URL=${shellQuote(apiBase)} DEVICE_ID=${shellQuote(deviceId)} SIMULATOR_TOKEN=${shellQuote(simulatorToken)} python simulator/device_simulator.py --scenario ${shellQuote(scenario)}`
    : "";

  async function copyDiagnostics() {
    try {
      await navigator.clipboard.writeText(
        JSON.stringify(
          {
            captured_at: new Date().toISOString(),
            api_origin: apiBase,
            health: health.data ?? null,
            devices:
              devices.data?.devices.map(
                ({ device_id, connected, simulated, last_seen }) => ({
                  device_id,
                  connected,
                  simulated,
                  last_seen,
                }),
              ) ?? null,
          },
          null,
          2,
        ),
      );
      setMessage(
        "Current diagnostics copied. No credentials or audio are included.",
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Clipboard access is unavailable",
      );
    }
  }

  async function copySimulatorCommand() {
    try {
      await navigator.clipboard.writeText(simulatorCommand);
      setMessage(
        "SIMULATED command copied. It does not test physical hardware.",
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Clipboard access is unavailable",
      );
    }
  }

  return (
    <>
      <PageHeading
        eyebrow="DEVELOPMENT"
        title="Developer tools"
        description="Inspect live API/device state, open the generated API schema, and run explicitly simulated protocol scenarios."
      />
      <section className="metrics">
        <article className="metric-card">
          <div className="metric-icon green">
            <Server size={18} />
          </div>
          <span>BACKEND HEALTH</span>
          <strong>
            {health.data?.status === "ok"
              ? "Healthy"
              : health.isLoading
                ? "Checking"
                : "Unavailable"}
          </strong>
          <small>{apiBase}/api/v1/health</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon blue">
            <Cpu size={18} />
          </div>
          <span>PHYSICAL DEVICES</span>
          <strong>
            {devices.data?.devices.filter((item) => !item.simulated).length ??
              "—"}
          </strong>
          <small>SIMULATED devices are excluded</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon purple">
            <Radio size={18} />
          </div>
          <span>CONNECTED PHYSICAL</span>
          <strong>
            {devices.data?.devices.filter(
              (item) => !item.simulated && item.connected,
            ).length ?? "—"}
          </strong>
          <small>Authenticated WebSocket sessions</small>
        </article>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>API reference</h2>
            <p>
              Generated from the running backend; endpoints still enforce their
              configured authentication.
            </p>
          </div>
          <button className="secondary" onClick={() => void copyDiagnostics()}>
            <Code2 size={16} />
            Copy diagnostics
          </button>
        </div>
        <div className="button-row">
          <a
            className="button-link"
            href={`${apiBase}/docs`}
            target="_blank"
            rel="noreferrer"
          >
            Open API docs
          </a>
          <a
            className="button-link secondary"
            href={`${apiBase}/openapi.json`}
            target="_blank"
            rel="noreferrer"
          >
            Open OpenAPI JSON
          </a>
        </div>
        {health.isError && (
          <div className="alert error" role="alert">
            {health.error.message}
          </div>
        )}
        {devices.isError && (
          <div className="alert error" role="alert">
            {devices.error.message}
          </div>
        )}
        {message && (
          <div className="response-box" role="status">
            {message}
          </div>
        )}
        {error && (
          <div className="alert error" role="alert">
            {error}
          </div>
        )}
      </section>
      <section className="panel">
        <h2>Protocol simulator</h2>
        <p>
          Run a simulator only for software protocol checks. Its traffic is
          explicitly <strong>SIMULATED</strong> and does not validate the
          physical board, microphone, speaker, models, or OTA rollback.
        </p>
        <div className="form-grid">
          <label>
            Simulator device ID
            <input
              value={deviceId}
              onChange={(event) => setDeviceId(event.target.value)}
              required
              pattern="[a-zA-Z0-9._-]+"
              maxLength={128}
            />
          </label>
          <label>
            Simulator token
            <input
              type="password"
              autoComplete="off"
              value={simulatorToken}
              onChange={(event) => setSimulatorToken(event.target.value)}
              required
            />
          </label>
          <label>
            Scenario
            <select
              value={scenario}
              onChange={(event) => setScenario(event.target.value)}
            >
              <option value="observe">Observe control events</option>
              <option value="reconnect">Reconnect</option>
              <option value="replay-gap">Replay-gap recovery</option>
            </select>
          </label>
        </div>
        {simulatorCommand && (
          <pre className="response-box">{simulatorCommand}</pre>
        )}
        <button
          disabled={!simulatorCommand}
          onClick={() => void copySimulatorCommand()}
        >
          <Code2 size={16} />
          Copy SIMULATED command
        </button>
        <p>
          The token stays in this page's transient input state and is not saved.
          PCM fixture playback is available from the CLI with{" "}
          <code>--scenario pcm --pcm-file &lt;fixture.pcm&gt;</code>.
        </p>
      </section>
    </>
  );
}

function WeatherPage() {
  const [location, setLocation] = useState("");
  const [selected, setSelected] = useState<{
    name: string;
    country?: string;
    latitude: number;
    longitude: number;
    timezone?: string;
  } | null>(null);
  const [error, setError] = useState("");
  const geocode = useQuery({
    queryKey: ["weather-location", location],
    queryFn: () =>
      fetchJson<{ locations: NonNullable<typeof selected>[] }>(
        `/api/v1/weather/geocode?name=${encodeURIComponent(location)}`,
      ),
    enabled: location.trim().length > 1,
    retry: false,
  });
  const forecast = useQuery({
    queryKey: ["weather-forecast", selected?.latitude, selected?.longitude],
    queryFn: () =>
      fetchJson<{
        current: Record<string, unknown>;
        daily: Record<string, unknown>;
      }>(
        "/api/v1/weather/current?latitude=" +
          selected?.latitude +
          "&longitude=" +
          selected?.longitude +
          "&timezone_name=" +
          encodeURIComponent(selected?.timezone ?? "auto"),
      ),
    enabled: Boolean(selected),
    retry: false,
  });
  return (
    <>
      <PageHeading
        eyebrow="WEATHER"
        title="Local forecast"
        description="Current conditions and three-day forecast from Open-Meteo; no API key is required."
      />
      <section className="panel">
        <label>
          Search location
          <input
            value={location}
            onChange={(event) => setLocation(event.target.value)}
            placeholder="City or town"
          />
        </label>
        {geocode.isError && (
          <div className="alert error">{geocode.error.message}</div>
        )}
        {geocode.data?.locations.map((item) => (
          <article className="event-row" key={`${item.name}-${item.latitude}`}>
            <div>
              <strong>
                {item.name}
                {item.country ? `, ${item.country}` : ""}
              </strong>
              <span>
                {item.latitude}, {item.longitude} ·{" "}
                {item.timezone ?? "local time"}
              </span>
            </div>
            <button
              className="secondary"
              onClick={() => {
                setSelected(item);
                setError("");
              }}
            >
              Forecast
            </button>
          </article>
        ))}
      </section>
      {selected && (
        <section className="panel">
          <h2>{selected.name} forecast</h2>
          {forecast.isLoading && (
            <div className="empty-state">Loading forecast…</div>
          )}
          {forecast.isError && (
            <div className="alert error">{forecast.error.message}</div>
          )}
          {forecast.data && (
            <pre className="response-box">
              {JSON.stringify(forecast.data, null, 2)}
            </pre>
          )}
        </section>
      )}
      {error && <div className="alert error">{error}</div>}
    </>
  );
}

type Telemetry = {
  device_id: string;
  timestamp: string;
  rms?: number;
  network_rtt_ms?: number;
  estimated_jitter_ms?: number;
  dropped_frames?: number;
  processing_ms?: number;
};

function MonitoringPage() {
  const [samples, setSamples] = useState<Telemetry[]>([]);
  const [error, setError] = useState("");
  const history = useQuery({
    queryKey: ["telemetry-history"],
    queryFn: () =>
      fetchJson<{ samples: Telemetry[] }>(
        "/api/v1/monitoring/telemetry?limit=120",
      ),
  });
  useEffect(() => {
    if (history.data) setSamples(history.data.samples);
  }, [history.data]);
  useEffect(() => {
    const token = localStorage.getItem(tokenKey);
    if (!token) return;
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    const socket = new WebSocket(
      `${scheme}//${new URL(apiBase).host}/api/v1/monitoring/stream`,
    );
    socket.addEventListener(
      "open",
      () => socket.send(JSON.stringify({ access_token: token })),
      { once: true },
    );
    socket.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data) as {
          type?: string;
          payload?: Telemetry;
        };
        if (message.type !== "telemetry.sample" || !message.payload) return;
        setSamples((current) => [...current, message.payload!].slice(-120));
      } catch {
        setError("Received malformed telemetry from the backend.");
      }
    };
    socket.onerror = () => setError("Live telemetry connection failed.");
    socket.onclose = () =>
      setError((current) => current || "Live telemetry disconnected.");
    return () => socket.close();
  }, []);
  const chartData = samples.map((sample) => ({
    time: new Date(sample.timestamp).toLocaleTimeString(),
    rms: sample.rms ?? null,
    network: sample.network_rtt_ms ?? null,
    jitter: sample.estimated_jitter_ms ?? null,
    processing: sample.processing_ms ?? null,
    dropped: sample.dropped_frames ?? null,
    device: sample.device_id,
  }));
  const latest = samples.at(-1);
  return (
    <>
      <PageHeading
        eyebrow="LIVE MONITORING"
        title="Telemetry stream"
        description="Authenticated live samples from connected devices: audio level, frame gaps, processing time, jitter, and ping round-trip time."
      />
      {error && <div className="alert error">{error}</div>}
      {history.isError && (
        <div className="alert error">{history.error.message}</div>
      )}
      <section className="metrics">
        <article className="metric-card">
          <div className="metric-icon purple">
            <Activity size={18} />
          </div>
          <span>MIC RMS</span>
          <strong>{latest?.rms ?? "—"}</strong>
          <small>latest audio frame</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon blue">
            <Wifi size={18} />
          </div>
          <span>NETWORK RTT</span>
          <strong>
            {latest?.network_rtt_ms == null
              ? "—"
              : `${latest.network_rtt_ms} ms`}
          </strong>
          <small>measured WebSocket ping</small>
        </article>
        <article className="metric-card">
          <div className="metric-icon green">
            <Gauge size={18} />
          </div>
          <span>DROPPED FRAMES</span>
          <strong>{latest?.dropped_frames ?? "—"}</strong>
          <small>sequence gaps observed</small>
        </article>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Audio and network timeline</h2>
            <p>
              {samples.length} recent samples · bounded in-memory telemetry; raw
              audio is never exposed
            </p>
          </div>
          <span className="live-tag">
            <i /> LIVE
          </span>
        </div>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--line)" />
              <XAxis dataKey="time" minTickGap={30} />
              <YAxis />
              <Tooltip
                contentStyle={{
                  background: "var(--panel)",
                  borderColor: "var(--line)",
                }}
              />
              <Line
                type="monotone"
                dataKey="rms"
                name="Audio RMS"
                stroke="#8b75f5"
                dot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="network"
                name="Network RTT (ms)"
                stroke="#55b5ef"
                dot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="processing"
                name="Processing (ms)"
                stroke="#55d6a5"
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </section>
      <section className="panel">
        <h2>Frame loss and jitter</h2>
        <div className="chart-wrap">
          <ResponsiveContainer width="100%" height={240}>
            <LineChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--line)" />
              <XAxis dataKey="time" minTickGap={30} />
              <YAxis />
              <Tooltip
                contentStyle={{
                  background: "var(--panel)",
                  borderColor: "var(--line)",
                }}
              />
              <Line
                type="stepAfter"
                dataKey="dropped"
                name="Cumulative gaps"
                stroke="#f08c78"
                dot={false}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="jitter"
                name="Estimated jitter (ms)"
                stroke="#e5b85c"
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </section>
    </>
  );
}

function DisplayStudioPage() {
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
  });
  const [deviceId, setDeviceId] = useState("");
  const [previewEmoji, setPreviewEmoji] = useState("🙂");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  async function savePage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const payload: { page: string; latitude?: number; longitude?: number } = {
      page: String(data.get("page")),
    };
    const latitude = String(data.get("latitude") ?? "").trim();
    const longitude = String(data.get("longitude") ?? "").trim();
    if (latitude && longitude) {
      payload.latitude = Number(latitude);
      payload.longitude = Number(longitude);
    }
    try {
      const result = await fetchJson<{ delivery: string; revision: number }>(
        `/api/v1/devices/${encodeURIComponent(deviceId)}/display/page`,
        { method: "PUT", body: JSON.stringify(payload) },
      );
      setMessage(
        `Live page ${result.delivery === "sent" ? "sent to device" : "saved for reconnect"} (revision ${result.revision}).`,
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Display page could not be changed",
      );
    }
  }
  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const payload = {
      state: data.get("state"),
      emoji: data.get("emoji"),
      animation: data.get("animation"),
      animation_duration_ms: Number(data.get("duration")),
      emoji_rules: {
        happy: data.get("happy_emoji"),
        listening: data.get("listening_emoji"),
      },
    };
    try {
      const result = await fetchJson<{ delivery: string; revision: number }>(
        `/api/v1/devices/${encodeURIComponent(deviceId)}/display`,
        {
          method: "PUT",
          body: JSON.stringify(payload),
        },
      );
      setMessage(
        `Configuration revision ${result.revision} ${result.delivery === "sent" ? "sent to device" : "saved for reconnect"}.`,
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : "Display settings could not be saved",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="DISPLAY & EXPRESSIONS"
        title="Display studio"
        description="Send live home, weather, reminders, notes, to-do, alarm, game, and notification pages to the connected ST7735. Voice commands can switch pages too."
      />
      <section className="panel">
        <form className="form-grid" onSubmit={savePage}>
          <label>
            Device
            <select
              value={deviceId}
              onChange={(event) => setDeviceId(event.target.value)}
              required
            >
              <option value="">Select a registered device</option>
              {devices.data?.devices
                .filter((device) => !device.simulated)
                .map((device) => (
                  <option key={device.device_id} value={device.device_id}>
                    {device.name} · {device.device_id}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Live page
            <select name="page">
              <option value="home">Home dashboard</option>
              <option value="weather">Weather</option>
              <option value="reminders">Reminders</option>
              <option value="notes">Notes</option>
              <option value="todos">To-dos</option>
              <option value="alarms">Alarms</option>
              <option value="games">Games</option>
              <option value="notifications">Notifications</option>
            </select>
          </label>
          <label>
            Weather latitude
            <input
              name="latitude"
              type="number"
              min="-90"
              max="90"
              step="any"
              placeholder="Optional, e.g. 23.81"
            />
          </label>
          <label>
            Weather longitude
            <input
              name="longitude"
              type="number"
              min="-180"
              max="180"
              step="any"
              placeholder="Optional, e.g. 90.41"
            />
          </label>
          <button disabled={!deviceId}>
            <Monitor size={16} /> Show page on device
          </button>
        </form>
        {message && <div className="response-box">{message}</div>}
        {error && <div className="alert error">{error}</div>}
        {devices.isError && (
          <div className="alert error">{devices.error.message}</div>
        )}
      </section>
      <section className="panel">
        <h2>Expression controls</h2>
        <form className="form-grid" onSubmit={save}>
          <label>
            Device
            <select
              value={deviceId}
              onChange={(event) => setDeviceId(event.target.value)}
              required
            >
              <option value="">Select a registered device</option>
              {devices.data?.devices
                .filter((device) => !device.simulated)
                .map((device) => (
                  <option key={device.device_id} value={device.device_id}>
                    {device.name} · {device.device_id}
                  </option>
                ))}
            </select>
          </label>
          <label>
            Current state
            <select name="state">
              <option>idle</option>
              <option>listening</option>
              <option>thinking</option>
              <option>speaking</option>
              <option>happy</option>
              <option>sad</option>
              <option>error</option>
              <option>sleeping</option>
            </select>
          </label>
          <label>
            Emoji expression
            <input
              name="emoji"
              value={previewEmoji}
              onChange={(event) => setPreviewEmoji(event.target.value)}
              maxLength={8}
              required
            />
          </label>
          <label>
            Animation
            <select name="animation">
              <option>none</option>
              <option>pulse</option>
              <option>bounce</option>
              <option>fade</option>
              <option>wave</option>
            </select>
          </label>
          <label>
            Animation duration (ms)
            <input
              name="duration"
              type="number"
              min="0"
              max="10000"
              defaultValue="600"
            />
          </label>
          <label>
            Happy emoji
            <input name="happy_emoji" defaultValue="😊" maxLength={8} />
          </label>
          <label>
            Listening emoji
            <input name="listening_emoji" defaultValue="🎙️" maxLength={8} />
          </label>
          <button disabled={!deviceId}>
            <Monitor size={16} /> Save expression
          </button>
        </form>
        {devices.isError && (
          <div className="alert error">{devices.error.message}</div>
        )}
      </section>
      <section className="panel">
        <h2>Preview</h2>
        <div className="display-preview">
          <span>{previewEmoji}</span>
          <small>SIMULATED UI PREVIEW · DOES NOT VALIDATE HARDWARE</small>
        </div>
      </section>
    </>
  );
}

function OTAManagementPage() {
  const releases = useQuery({
    queryKey: ["ota-releases"],
    queryFn: () =>
      fetchJson<{
        releases: {
          id: string;
          version: string;
          channel: string;
          sha256: string;
          size_bytes: number;
          hardware_target: string;
          created_at: string;
        }[];
      }>("/api/v1/ota/releases"),
  });
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => fetchJson<{ devices: Device[] }>("/api/v1/devices"),
  });
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function upload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = event.currentTarget;
    try {
      const result = await fetchJson<{ version: string; sha256: string }>(
        "/api/v1/ota/releases",
        { method: "POST", body: new FormData(form) },
      );
      setMessage(
        `Release ${result.version} stored with SHA-256 ${result.sha256}.`,
      );
      form.reset();
      await releases.refetch();
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Firmware upload failed",
      );
    }
  }
  async function deploy(releaseId: string, deviceId: string) {
    try {
      const result = await fetchJson<{ delivery: string }>(
        `/api/v1/ota/releases/${releaseId}/deploy/${encodeURIComponent(deviceId)}`,
        { method: "POST" },
      );
      setMessage(
        `Deployment manifest ${result.delivery}. Firmware must implement the OTA receiver before the image can be installed.`,
      );
      setError("");
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : "Deployment request failed",
      );
    }
  }
  return (
    <>
      <PageHeading
        eyebrow="FIRMWARE DELIVERY"
        title="OTA releases"
        description="Upload ESP image binaries, assign release channels, verify SHA-256 metadata, and send an authenticated device manifest. Firmware-side OTA installation is still required."
      />
      <section className="panel">
        <h2>Upload release image</h2>
        <form className="form-grid" onSubmit={upload}>
          <label>
            Version
            <input
              name="version"
              required
              pattern="[a-zA-Z0-9][a-zA-Z0-9._+-]*"
              maxLength={64}
            />
          </label>
          <label>
            Channel
            <select name="channel">
              <option>development</option>
              <option>beta</option>
              <option>stable</option>
            </select>
          </label>
          <label>
            Hardware target
            <input
              name="hardware_target"
              defaultValue="esp32-s3-n16r8"
              required
              maxLength={64}
            />
          </label>
          <label>
            Firmware binary
            <input
              name="firmware"
              type="file"
              accept=".bin,application/octet-stream"
              required
            />
          </label>
          <button>
            <Upload size={16} /> Upload and hash
          </button>
        </form>
        {message && <div className="response-box">{message}</div>}
        {error && <div className="alert error">{error}</div>}
      </section>
      <section className="panel">
        <h2>Release channels and deployment</h2>
        {releases.isError && (
          <div className="alert error">{releases.error.message}</div>
        )}
        {releases.data?.releases.map((release) => (
          <article className="event-row" key={release.id}>
            <div>
              <strong>
                {release.version} · {release.channel}
              </strong>
              <span>
                {release.hardware_target} ·{" "}
                {(release.size_bytes / 1024).toFixed(1)} KiB · SHA-256{" "}
                {release.sha256}
              </span>
              <span>{new Date(release.created_at).toLocaleString()}</span>
            </div>
            <div className="button-row">
              <select
                aria-label={`Device for ${release.version}`}
                id={`target-${release.id}`}
              >
                <option value="">Choose device</option>
                {devices.data?.devices
                  .filter((device) => !device.simulated)
                  .map((device) => (
                    <option key={device.device_id} value={device.device_id}>
                      {device.device_id}
                    </option>
                  ))}
              </select>
              <button
                className="secondary"
                onClick={() => {
                  const selected = document.getElementById(
                    `target-${release.id}`,
                  ) as HTMLSelectElement | null;
                  if (selected?.value) void deploy(release.id, selected.value);
                  else setError("Select a physical device first.");
                }}
              >
                Deploy manifest
              </button>
            </div>
          </article>
        ))}
      </section>
    </>
  );
}

type VisionStatus = {
  enabled: boolean;
  source: string;
  camera_active: boolean;
  remote_frames: boolean;
  last_frame_age_seconds: number | null;
  owner_enrolled: boolean;
  owner_present: boolean;
  recording: string | null;
  recordings_saved: number;
  faces_remembered: number;
  embeddings: number;
  telegram_configured: boolean;
  face_enrollment: string;
};
type VisionPerson = {
  id: string;
  name: string;
  role: string;
  sightings: number;
  embeddings: number;
  last_seen_at: string | null;
};
type VisionEventRow = {
  id: string;
  created_at: string;
  kind: string;
  detail: string;
  media_path: string | null;
  alerted: boolean;
};
type VisionMemoryRow = {
  id: string;
  kind: string;
  label: string;
  content: string;
  created_at: string;
};

function VisionPage() {
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [snapshot, setSnapshot] = useState("");
  const [mergeSource, setMergeSource] = useState("");
  const [mergeTarget, setMergeTarget] = useState("");
  const status = useQuery({
    queryKey: ["vision-status"],
    queryFn: () => fetchJson<VisionStatus>("/api/v1/vision/status"),
    refetchInterval: 3_000,
  });
  const people = useQuery({
    queryKey: ["vision-people"],
    queryFn: () => fetchJson<VisionPerson[]>("/api/v1/vision/people"),
    refetchInterval: 5_000,
  });
  const events = useQuery({
    queryKey: ["vision-events"],
    queryFn: () =>
      fetchJson<VisionEventRow[]>("/api/v1/vision/events?limit=50"),
    refetchInterval: 5_000,
  });
  const memories = useQuery({
    queryKey: ["vision-memories"],
    queryFn: () => fetchJson<VisionMemoryRow[]>("/api/v1/vision/memories"),
  });
  const media = useQuery({
    queryKey: ["vision-media"],
    queryFn: () =>
      fetchJson<{ path: string; bytes: number }[]>("/api/v1/vision/media"),
  });

  async function run(action: () => Promise<unknown>, success: string) {
    setError("");
    setMessage("");
    try {
      await action();
      setMessage(success);
      await Promise.all([
        status.refetch(),
        people.refetch(),
        events.refetch(),
        memories.refetch(),
        media.refetch(),
      ]);
    } catch (caught) {
      setError((caught as Error).message);
    }
  }
  async function loadBlob(path: string) {
    const response = await fetch(`${apiBase}${path}`, {
      headers: {
        Authorization: `Bearer ${localStorage.getItem(tokenKey) ?? ""}`,
      },
    });
    if (!response.ok)
      throw new Error(`Backend returned HTTP ${response.status}`);
    return URL.createObjectURL(await response.blob());
  }
  async function showSnapshot() {
    setError("");
    try {
      setSnapshot(await loadBlob("/api/v1/vision/snapshot"));
    } catch (caught) {
      setError((caught as Error).message);
    }
  }
  async function openMedia(path: string) {
    try {
      window.open(await loadBlob(`/api/v1/vision/media/${path}`), "_blank");
    } catch (caught) {
      setError((caught as Error).message);
    }
  }
  const post = (path: string, body?: unknown) =>
    fetchJson(`/api/v1/vision${path}`, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  const current = status.data;

  return (
    <>
      <PageHeading
        eyebrow="Security"
        title="Camera & memory"
        description="Face recognition, intruder alerts, recordings and remembered objects, text and home layout. Face embeddings are stored, not photos."
      />
      {message && <div className="response-box">{message}</div>}
      {error && <div className="alert error">{error}</div>}
      {status.isError && (
        <div className="alert error">{status.error.message}</div>
      )}
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Status</h2>
            <p>
              {current
                ? `${current.camera_active ? "Camera active" : "Camera inactive"} · source ${current.source}${current.remote_frames ? " (ESP32 frames arriving)" : ""} · owner ${current.owner_enrolled ? (current.owner_present ? "present" : "away") : "not enrolled"} · face enrollment ${current.face_enrollment} · ${current.faces_remembered} faces / ${current.embeddings} embeddings · Telegram ${current.telegram_configured ? "configured" : "not configured"}${current.recording ? ` · recording (${current.recording})` : ""}`
                : "Loading"}
            </p>
          </div>
        </div>
        <div className="form-grid">
          <button
            onClick={() =>
              run(
                () => post("/monitor", { enabled: !current?.enabled }),
                current?.enabled
                  ? "Camera security stopped"
                  : "Camera security started",
              )
            }
          >
            {current?.enabled
              ? "Stop camera security"
              : "Start camera security"}
          </button>
          <button className="secondary" onClick={showSnapshot}>
            Live snapshot
          </button>
          <button
            className="secondary"
            onClick={() => run(() => post("/photo"), "Photo taken")}
          >
            Take photo
          </button>
          <button
            className="secondary"
            onClick={() =>
              run(
                () =>
                  post("/recording", {
                    action: current?.recording ? "stop" : "start",
                    seconds: 60,
                  }),
                current?.recording ? "Recording stopped" : "Recording 60 s",
              )
            }
          >
            {current?.recording ? "Stop recording" : "Record 60 s"}
          </button>
          <button
            className="secondary"
            onClick={() =>
              run(() => post("/telegram/test"), "Telegram test sent")
            }
          >
            Test Telegram
          </button>
        </div>
        {snapshot && (
          <img
            src={snapshot}
            alt="Live camera snapshot"
            style={{ maxWidth: "100%", marginTop: 12, borderRadius: 8 }}
          />
        )}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>People</h2>
            <p>Enrol the person currently in front of the camera.</p>
          </div>
        </div>
        <form
          className="form-grid"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            const form = event.currentTarget;
            const data = new FormData(form);
            run(
              () =>
                post("/enroll", {
                  name: String(data.get("name")),
                  role: String(data.get("role")),
                }),
              "Face enrolled",
            ).then(() => form.reset());
          }}
        >
          <label>
            Name
            <input name="name" required maxLength={80} />
          </label>
          <label>
            Role
            <select name="role">
              <option value="owner">Owner</option>
              <option value="known">Known person</option>
            </select>
          </label>
          <button>Enrol face</button>
        </form>
        <form
          className="form-grid"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            const source = people.data?.find(
              (person) => person.id === mergeSource,
            );
            const target = people.data?.find(
              (person) => person.id === mergeTarget,
            );
            if (
              source &&
              target &&
              window.confirm(
                `Merge ${source.name} into ${target.name}? ${source.name}'s identity will be removed; embeddings and event references will move to ${target.name}. This cannot be undone.`,
              )
            ) {
              void run(
                () =>
                  post(`/people/${source.id}/merge`, { target_id: target.id }),
                "People merged",
              );
            }
          }}
        >
          <label>
            Merge identity
            <select
              value={mergeSource}
              onChange={(event) => setMergeSource(event.target.value)}
              required
            >
              <option value="">Select person</option>
              {people.data?.map((person) => (
                <option key={person.id} value={person.id}>
                  {person.name} ({person.role})
                </option>
              ))}
            </select>
          </label>
          <label>
            Keep identity
            <select
              value={mergeTarget}
              onChange={(event) => setMergeTarget(event.target.value)}
              required
            >
              <option value="">Select person</option>
              {people.data
                ?.filter((person) => person.id !== mergeSource)
                .map((person) => (
                  <option key={person.id} value={person.id}>
                    {person.name} ({person.role})
                  </option>
                ))}
            </select>
          </label>
          <button
            disabled={
              !mergeSource || !mergeTarget || mergeSource === mergeTarget
            }
          >
            Merge identities
          </button>
        </form>
        {people.data?.map((person) => (
          <div className="event-row" key={person.id}>
            <div>
              <strong>{person.name}</strong>
              <span>
                {person.role} · {person.embeddings} embeddings · seen{" "}
                {person.sightings}×
                {person.last_seen_at
                  ? ` · last ${new Date(person.last_seen_at).toLocaleString()}`
                  : ""}
              </span>
            </div>
            <div>
              <button
                className="secondary"
                onClick={() => {
                  const name = window.prompt("Name", person.name);
                  if (name)
                    run(
                      () =>
                        fetchJson(`/api/v1/vision/people/${person.id}`, {
                          method: "PATCH",
                          body: JSON.stringify({ name }),
                        }),
                      "Renamed",
                    );
                }}
              >
                Rename
              </button>
              {person.role === "stranger" && (
                <button
                  className="secondary"
                  onClick={() =>
                    run(
                      () =>
                        fetchJson(`/api/v1/vision/people/${person.id}`, {
                          method: "PATCH",
                          body: JSON.stringify({ role: "known" }),
                        }),
                      "Marked as known",
                    )
                  }
                >
                  Mark known
                </button>
              )}
              <button
                className="secondary"
                onClick={() =>
                  run(
                    () =>
                      fetchJson(`/api/v1/vision/people/${person.id}`, {
                        method: "DELETE",
                      }),
                    "Face forgotten",
                  )
                }
              >
                Forget
              </button>
            </div>
          </div>
        ))}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Memories</h2>
            <p>
              Home organisation notes and paper text. Objects are remembered via
              the camera.
            </p>
          </div>
        </div>
        <form
          className="form-grid"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            const form = event.currentTarget;
            const data = new FormData(form);
            run(
              () =>
                post("/memories", {
                  kind: String(data.get("kind")),
                  label: String(data.get("label")),
                  content: String(data.get("content")),
                }),
              "Memory saved",
            ).then(() => form.reset());
          }}
        >
          <label>
            Kind
            <select name="kind">
              <option value="home">Home / location</option>
              <option value="text">Text note</option>
            </select>
          </label>
          <label>
            Label
            <input name="label" required maxLength={120} />
          </label>
          <label>
            Content
            <input name="content" required />
          </label>
          <button>Save memory</button>
        </form>
        <form
          className="form-grid"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            const form = event.currentTarget;
            const label = String(new FormData(form).get("label"));
            run(
              () => post("/memories/object", { label }),
              `Object "${label}" remembered`,
            ).then(() => form.reset());
          }}
        >
          <label>
            Object in view
            <input
              name="label"
              required
              maxLength={120}
              placeholder="e.g. my phone"
            />
          </label>
          <button>Remember object</button>
        </form>
        <form
          className="form-grid"
          onSubmit={(event: FormEvent<HTMLFormElement>) => {
            event.preventDefault();
            const form = event.currentTarget;
            const label = String(new FormData(form).get("label"));
            run(
              () =>
                fetchJson(
                  `/api/v1/vision/memories/read-text?label=${encodeURIComponent(label)}`,
                  { method: "POST" },
                ),
              "Paper read and remembered",
            ).then(() => form.reset());
          }}
        >
          <label>
            Paper title
            <input
              name="label"
              maxLength={120}
              placeholder="e.g. electricity bill"
            />
          </label>
          <button>Read paper (OCR)</button>
        </form>
        {memories.data?.map((item) => (
          <div className="event-row" key={item.id}>
            <div>
              <strong>
                [{item.kind}] {item.label}
              </strong>
              <span>{item.content}</span>
            </div>
            <button
              className="secondary"
              onClick={() =>
                run(
                  () =>
                    fetchJson(`/api/v1/vision/memories/${item.id}`, {
                      method: "DELETE",
                    }),
                  "Memory deleted",
                )
              }
            >
              Delete
            </button>
          </div>
        ))}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Security log</h2>
          </div>
        </div>
        {events.data?.map((item) => (
          <div className="event-row" key={item.id}>
            <div>
              <strong>
                {item.kind}
                {item.alerted ? " · alerted" : ""}
              </strong>
              <span>
                {new Date(item.created_at).toLocaleString()} · {item.detail}
              </span>
            </div>
            {item.media_path && (
              <button
                className="secondary"
                onClick={() => openMedia(item.media_path as string)}
              >
                Open media
              </button>
            )}
          </div>
        ))}
      </section>
      <section className="panel">
        <div className="panel-heading">
          <div>
            <h2>Recordings & photos</h2>
          </div>
        </div>
        {media.data?.map((item) => (
          <div className="event-row" key={item.path}>
            <div>
              <strong>{item.path}</strong>
              <span>{(item.bytes / 1024).toFixed(0)} KB</span>
            </div>
            <button className="secondary" onClick={() => openMedia(item.path)}>
              Open
            </button>
          </div>
        ))}
      </section>
    </>
  );
}

function PageHeading({
  eyebrow,
  title,
  description,
}: {
  eyebrow: string;
  title: string;
  description: string;
}) {
  return (
    <section className="page-heading">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="subheading">{description}</p>
      </div>
    </section>
  );
}

export default function App() {
  const [authenticated, setAuthenticated] = useState(
    Boolean(localStorage.getItem(tokenKey)),
  );
  const navigate = useNavigate();
  if (!authenticated) return <Login onLogin={() => setAuthenticated(true)} />;
  function logout() {
    localStorage.removeItem(tokenKey);
    setAuthenticated(false);
    navigate("/");
  }
  return (
    <Shell onLogout={logout}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/devices" element={<DevicesPage />} />
        <Route path="/monitoring" element={<MonitoringPage />} />
        <Route path="/display" element={<DisplayStudioPage />} />
        <Route path="/ota" element={<OTAManagementPage />} />
        <Route path="/calendar" element={<CalendarPage />} />
        <Route path="/alarms" element={<AlarmsPage />} />
        <Route path="/settings" element={<SettingsPage />} />
        <Route path="/developer" element={<DeveloperPage />} />
        <Route path="/games" element={<GamesPage />} />
        <Route path="/game-history" element={<GameHistoryPage />} />
        <Route path="/brain" element={<BrainPage />} />
        <Route path="/ai" element={<AIPage />} />
        <Route path="/voice" element={<VoicePage />} />
        <Route path="/personalization" element={<PersonalizationPage />} />
        <Route path="/biometric" element={<BiometricPage />} />
        <Route path="/integrations" element={<IntegrationsPage />} />
        <Route path="/credentials" element={<CredentialsPage />} />
        <Route path="/notifications" element={<NotificationsPage />} />
        <Route path="/weather" element={<WeatherPage />} />
        <Route path="/automation" element={<AutomationPage />} />
        <Route path="/vision" element={<VisionPage />} />
        <Route path="/audit" element={<AuditPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Shell>
  );
}
