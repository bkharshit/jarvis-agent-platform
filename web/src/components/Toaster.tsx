import { useToastStore, type ToastKind } from "@/stores/toast";

const KIND_STYLES: Record<ToastKind, string> = {
  error: "border-red-800 bg-red-950 text-red-100",
  success: "border-green-800 bg-green-950 text-green-100",
  info: "border-neutral-700 bg-neutral-900 text-neutral-100",
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
          className={`flex items-start justify-between gap-3 rounded border px-3 py-2 text-sm shadow-lg ${KIND_STYLES[t.kind]}`}
        >
          <span className="min-w-0 break-words">{t.message}</span>
          <button
            type="button"
            onClick={() => dismiss(t.id)}
            aria-label="Dismiss notification"
            className="shrink-0 cursor-pointer text-current/60 hover:text-current"
          >
            ✕
          </button>
        </div>
      ))}
    </div>
  );
}