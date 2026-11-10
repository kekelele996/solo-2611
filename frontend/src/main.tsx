import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { lazy, StrictMode, Suspense } from "react";
import type { ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import { AppShell, NotFoundPage } from "./components/AppShell";
import { ToastProvider } from "./components/toast";
import { LoadingLines } from "./components/ui";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/app.css";

const DashboardPage = lazy(() => import("./pages/DashboardPage").then((module) => ({ default: module.DashboardPage })));
const TasksPage = lazy(() => import("./pages/TasksPage").then((module) => ({ default: module.TasksPage })));
const TaskDetailPage = lazy(() =>
  import("./pages/TaskDetailPage").then((module) => ({ default: module.TaskDetailPage })),
);
const SourcePage = lazy(() => import("./pages/SourcePage").then((module) => ({ default: module.SourcePage })));
const ExportPage = lazy(() => import("./pages/ExportPage").then((module) => ({ default: module.ExportPage })));
const SystemPage = lazy(() => import("./pages/SystemPage").then((module) => ({ default: module.SystemPage })));

function deferred(element: ReactNode) {
  return <Suspense fallback={<div className="page"><LoadingLines rows={8} /></div>}>{element}</Suspense>;
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2500,
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    errorElement: <NotFoundPage />,
    children: [
      { index: true, element: deferred(<DashboardPage />) },
      { path: "tasks", element: deferred(<TasksPage />) },
      { path: "tasks/:id", element: deferred(<TaskDetailPage />) },
      { path: "source", element: deferred(<SourcePage />) },
      { path: "export", element: deferred(<ExportPage />) },
      { path: "system", element: deferred(<SystemPage />) },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
]);

const rootElement = document.getElementById("root");
if (!rootElement) throw new Error("缺少 #root 容器");

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ToastProvider>
        <RouterProvider router={router} />
      </ToastProvider>
    </QueryClientProvider>
  </StrictMode>,
);
