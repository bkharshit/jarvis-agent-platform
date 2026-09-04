import { Navigate, Route, Routes } from "react-router";

import { Layout } from "@/app/Layout";
import { sectionRoutes } from "@/app/routes";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        {sectionRoutes().map((route) => (
          <Route key={route.path} path={route.path} element={route.element} />
        ))}
        <Route path="/" element={<Navigate to="/agents" replace />} />
        <Route
          path="*"
          element={
            <div className="px-6 py-16 text-center text-neutral-400">
              Not found.
            </div>
          }
        />
      </Route>
    </Routes>
  );
}