import { Link } from "react-router";
import { ChevronRight } from "lucide-react";

export interface BreadcrumbItem {
  label: string;
  to?: string;
}

export function Breadcrumbs({ items }: { items: BreadcrumbItem[] }) {
  return (
    <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-xs text-neutral-500">
      <ol className="flex items-center gap-1">
        {items.map((item, index) => {
          const last = index === items.length - 1;
          return (
            <li key={item.label} className="flex items-center gap-1">
              {item.to && !last ? (
                <Link to={item.to} className="transition-colors hover:text-neutral-300">
                  {item.label}
                </Link>
              ) : (
                <span className={last ? "font-medium text-neutral-300" : undefined}>
                  {item.label}
                </span>
              )}
              {!last && <ChevronRight aria-hidden className="h-3 w-3 text-neutral-700" />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}