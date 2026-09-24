import type { ReactNode } from "react";
import { LayoutGrid, Search, Table } from "lucide-react";

import { Breadcrumbs, type BreadcrumbItem } from "@/components/ui/Breadcrumbs";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/utils";

// Shared catalog chrome: title, optional search + grid/table view switcher,
// and the page's primary action. Everything above the list, in one place.

export interface PageHeaderProps {
  title: string;
  description?: string;
  breadcrumbs?: BreadcrumbItem[];
  search?: {
    value: string;
    onChange: (value: string) => void;
    placeholder: string;
  };
  view?: {
    value: "grid" | "table";
    onChange: (value: "grid" | "table") => void;
  };
  actions?: ReactNode;
}

export function PageHeader({ title, description, breadcrumbs, search, view, actions }: PageHeaderProps) {
  return (
    <div className="flex flex-col gap-3 border-b border-neutral-800/80 px-6 py-5">
      {breadcrumbs && <Breadcrumbs items={breadcrumbs} />}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {description && <p className="mt-0.5 text-sm text-neutral-400">{description}</p>}
        </div>
        {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
      </div>
      {(search || view) && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          {search && (
            <div className="relative w-full max-w-xs">
              <Search
                aria-hidden
                className="pointer-events-none absolute top-1/2 left-2.5 h-4 w-4 -translate-y-1/2 text-neutral-500"
              />
              <Input
                aria-label="Search"
                value={search.value}
                onChange={(event) => search.onChange(event.target.value)}
                placeholder={search.placeholder}
                className="pl-8"
              />
            </div>
          )}
          {view && <ViewSwitcher value={view.value} onChange={view.onChange} />}
        </div>
      )}
    </div>
  );
}

export function ViewSwitcher({
  value,
  onChange,
}: {
  value: "grid" | "table";
  onChange: (value: "grid" | "table") => void;
}) {
  const options = [
    { key: "grid" as const, icon: LayoutGrid, label: "Grid view" },
    { key: "table" as const, icon: Table, label: "Table view" },
  ];
  return (
    <div
      role="group"
      aria-label="View"
      className="inline-flex items-center gap-0.5 rounded-lg bg-neutral-900 p-0.5"
    >
      {options.map(({ key, icon: Icon, label }) => (
        <button
          key={key}
          type="button"
          aria-label={label}
          aria-pressed={value === key}
          onClick={() => onChange(key)}
          className={cn(
            "inline-flex h-7 w-7 cursor-pointer items-center justify-center rounded-md transition-colors",
            value === key
              ? "bg-neutral-800 text-neutral-100"
              : "text-neutral-500 hover:text-neutral-300",
          )}
        >
          <Icon aria-hidden className="h-4 w-4" />
        </button>
      ))}
    </div>
  );
}