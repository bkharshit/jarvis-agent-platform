import { useToastStore, type ToastKind } from "@/stores/toast";

const KIND_STYLES: Record<ToastKind, string> = {
  error: "border-rose-500/30 bg-neutral-900/80 text-rose-200",
  success: "border-emerald-500/30 bg-neutral-900/80 text-emerald-200",
  info: "border-neutral-700/60 bg-neutral-900/80 text-neutral-200",
};

export function Toaster() {
  const toasts = useToastStore((state) => state.toasts);
  const dismiss = useToastStore((state) => state.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div
      aria-live="polite"
      className="fixed bottom-4 right-4 z-50 flex w-80 flex-col gap-2"
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`flex items-start justify-between gap-3 rounded-xl border px-3 py-2 text-sm shadow-lg backdrop-blur ${KIND_STYLES[t.kind]}`}
        >
          <span className="min-w-0 break-words">{t.message}</span>
          <button
            type="button"
            onClick={() => dismiss(t.id)}
            aria-label="Dismiss notification"
            className="shrink-0 cursor-pointer text-current/60 transition-colors hover:text-current"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}