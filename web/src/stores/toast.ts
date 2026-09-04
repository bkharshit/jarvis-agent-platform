import { create } from "zustand";

// Hand-rolled toast state (decision 2 — no component library). Components
// call the module-level `toast()`; <Toaster/> renders the stack.

export type ToastKind = "error" | "success" | "info";

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastState {
  toasts: Toast[];
  push: (kind: ToastKind, message: string) => void;
  dismiss: (id: number) => void;
}

const AUTO_DISMISS_MS = 5000;
let nextId = 1;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  push: (kind, message) => {
    const id = nextId++;
    set((state) => ({ toasts: [...state.toasts, { id, kind, message }] }));
    setTimeout(() => useToastStore.getState().dismiss(id), AUTO_DISMISS_MS);
  },
  dismiss: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

export function toast(kind: ToastKind, message: string): void {
  useToastStore.getState().push(kind, message);
}