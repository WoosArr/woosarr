// Shared UI building blocks (plan P6-04): badges, buttons, modals, confirm
// dialogs, toasts, empty/loading states, data hooks.
import {
  cloneElement,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
  type ReactElement,
  type ReactNode,
} from "react";
import { ApiError } from "./api";
import { Icon, type IconName } from "./icons";

// ---- Badges: always icon + text (N8) ----
export type Tone = "ok" | "warn" | "danger" | "info" | "accent" | "neutral";
const toneIcon: Record<Tone, IconName> = {
  ok: "check",
  warn: "alert",
  danger: "x",
  info: "info",
  accent: "info",
  neutral: "minus",
};

export function Badge({ tone, icon, children, title }: { tone: Tone; icon?: IconName; children: ReactNode; title?: string }) {
  return (
    <span className={`badge ${tone}`} title={title}>
      <Icon name={icon ?? toneIcon[tone]} size={13} />
      {children}
    </span>
  );
}

export function VerifiedBadge({ verified }: { verified: boolean }) {
  return verified ? (
    <Badge tone="ok" icon="shieldCheck" title="Linked by Plex sign-in or by an admin">
      Verified
    </Badge>
  ) : (
    <Badge tone="warn" icon="shield" title="User typed a Plex username; not proven">
      Unverified
    </Badge>
  );
}

const sourceTone: Record<string, Tone> = { role: "accent", manual: "info", exempt: "neutral", external: "neutral" };
const sourceIcon: Record<string, IconName> = { role: "roles", manual: "user", exempt: "lock", external: "link" };
const sourceText: Record<string, string> = {
  role: "Granted by a Discord role",
  manual: "Granted manually by an admin",
  exempt: "Exempt library: role changes never touch it",
  external: "Shared in Plex directly; WooSarr leaves it alone unless a role manages the library",
};

export function SourceBadge({ source }: { source: string }) {
  return (
    <Badge tone={sourceTone[source] ?? "neutral"} icon={sourceIcon[source]} title={sourceText[source]}>
      {source}
    </Badge>
  );
}

export function RoleChip({ name, color }: { name: string; color: number }) {
  const hex = color ? `#${color.toString(16).padStart(6, "0")}` : "transparent";
  return (
    <span className="badge neutral">
      <span className="role-dot" style={{ background: hex }} aria-hidden="true" />
      {name}
    </span>
  );
}

// ---- Loading / empty / errors ----
export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span className="row muted" role="status">
      <span className="spinner" aria-hidden="true" />
      {label}…
    </span>
  );
}

export function Empty({ icon = "info", title, children }: { icon?: IconName; title: string; children?: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} size={32} />
      <h3>{title}</h3>
      {children ? <div className="small">{children}</div> : null}
    </div>
  );
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  const rid = error instanceof ApiError ? error.requestId : null;
  return (
    <div className="banner danger" role="alert">
      <Icon name="alert" />
      <div>
        <strong>Something went wrong.</strong> {msg}
        {rid ? <div className="small muted">Request id: <code>{rid}</code> (search for it on the Logs page)</div> : null}
      </div>
    </div>
  );
}

// ---- Data hook ----
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);
  const seq = useRef(0);
  const reload = useCallback(() => {
    const mine = ++seq.current;
    setLoading(true);
    return loader()
      .then((d) => {
        if (mine === seq.current) {
          setData(d);
          setError(null);
        }
      })
      .catch((e) => mine === seq.current && setError(e))
      .finally(() => mine === seq.current && setLoading(false));
  }, deps);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}

// ---- Modal ----
export function Modal({
  title,
  onClose,
  children,
  actions,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  actions?: ReactNode;
}) {
  const id = useId();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const prev = document.activeElement as HTMLElement | null;
    const first = ref.current?.querySelector<HTMLElement>("input, select, textarea, button:not(.modal-close)");
    (first ?? ref.current)?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "Tab" && ref.current) {
        const items = ref.current.querySelectorAll<HTMLElement>(
          "a[href], button:not(:disabled), input:not(:disabled), select, textarea, [tabindex]:not([tabindex='-1'])",
        );
        if (!items.length) return;
        const firstEl = items[0];
        const last = items[items.length - 1];
        if (e.shiftKey && document.activeElement === firstEl) {
          last.focus();
          e.preventDefault();
        } else if (!e.shiftKey && document.activeElement === last) {
          firstEl.focus();
          e.preventDefault();
        }
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      prev?.focus();
    };
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby={id} ref={ref} tabIndex={-1}>
        <div className="row spread">
          <h2 id={id}>{title}</h2>
          <button className="btn ghost icon-btn modal-close" onClick={onClose} aria-label="Close">
            <Icon name="x" />
          </button>
        </div>
        {children}
        {actions ? <div className="modal-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

// ---- Confirm dialog ----
interface ConfirmOptions {
  title: string;
  body: ReactNode;
  confirmLabel?: string;
  danger?: boolean;
}

const ConfirmCtx = createContext<(o: ConfirmOptions) => Promise<boolean>>(async () => false);

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<(ConfirmOptions & { resolve: (v: boolean) => void }) | null>(null);
  const confirm = useCallback(
    (o: ConfirmOptions) => new Promise<boolean>((resolve) => setState({ ...o, resolve })),
    [],
  );
  const close = (v: boolean) => {
    state?.resolve(v);
    setState(null);
  };
  return (
    <ConfirmCtx.Provider value={confirm}>
      {children}
      {state ? (
        <Modal
          title={state.title}
          onClose={() => close(false)}
          actions={
            <>
              <button className="btn" onClick={() => close(false)}>
                Cancel
              </button>
              <button className={`btn ${state.danger ? "danger" : "primary"}`} onClick={() => close(true)}>
                {state.confirmLabel ?? "Confirm"}
              </button>
            </>
          }
        >
          <div>{state.body}</div>
        </Modal>
      ) : null}
    </ConfirmCtx.Provider>
  );
}

export const useConfirm = () => useContext(ConfirmCtx);

// ---- Toasts ----
interface Toast {
  id: number;
  tone: "ok" | "danger" | "info";
  text: string;
}
const ToastCtx = createContext<(tone: Toast["tone"], text: string) => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);
  const push = useCallback((tone: Toast["tone"], text: string) => {
    const id = next.current++;
    setToasts((t) => [...t, { id, tone, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "danger" ? 8000 : 4000);
  }, []);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.tone}`}>
            <span className={`toast-icon ${t.tone}`}>
              <Icon name={t.tone === "ok" ? "check" : t.tone === "danger" ? "alert" : "info"} />
            </span>
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export function useToast() {
  const push = useContext(ToastCtx);
  return {
    ok: (t: string) => push("ok", t),
    info: (t: string) => push("info", t),
    error: (e: unknown) => push("danger", e instanceof Error ? e.message : String(e)),
  };
}

// ---- Misc ----
export function Switch({
  checked,
  onChange,
  label,
  disabled,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  disabled?: boolean;
}) {
  return (
    <label className="switch">
      <input type="checkbox" role="switch" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} />
      <span>{label}</span>
    </label>
  );
}

export function PageHead({ title, sub, children }: { title: string; sub?: ReactNode; children?: ReactNode }) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {sub ? <p>{sub}</p> : null}
      </div>
      {children ? <div className="row">{children}</div> : null}
    </div>
  );
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "never";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

export function fmtLogTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function ago(iso: string | null | undefined): string {
  if (!iso) return "never";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

// ---- Form field: short accessible label, hint linked via aria-describedby ----
export function Field({
  label,
  hint,
  error,
  children,
}: {
  label: ReactNode;
  hint?: ReactNode;
  error?: ReactNode;
  children: ReactElement<{ id?: string; "aria-describedby"?: string; "aria-invalid"?: boolean }>;
}) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const errId = error ? `${id}-err` : undefined;
  const describedBy = [hintId, errId].filter(Boolean).join(" ") || undefined;
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      {hint ? <span id={hintId} className="hint">{hint}</span> : null}
      {cloneElement(children, { id, "aria-describedby": describedBy, "aria-invalid": error ? true : undefined })}
      {error ? <span id={errId} className="hint" style={{ color: "var(--danger)" }}>{error}</span> : null}
    </div>
  );
}
