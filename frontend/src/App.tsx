import { Navigate, Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import DependenciesPage from "./pages/Dependencies";
import DependencyDetailPage from "./pages/DependencyDetail";
import OverviewPage from "./pages/Overview";
import { ApprovalsPage, UpgradesPage } from "./pages/Upgrades";
import UpgradeDetailPage from "./pages/UpgradeDetail";

export default function App() {
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<OverviewPage />} />
        <Route path="dependencies" element={<DependenciesPage />} />
        <Route path="dependencies/:id" element={<DependencyDetailPage />} />
        <Route path="upgrades" element={<UpgradesPage />} />
        <Route path="upgrades/:id" element={<UpgradeDetailPage />} />
        <Route path="approvals" element={<ApprovalsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
