import { Navigate, Route, Routes } from "react-router-dom";

import { Shell } from "./components/Shell";
import DependenciesPage from "./pages/Dependencies";
import DependencyDetailPage from "./pages/DependencyDetail";
import OverviewPage from "./pages/Overview";
import ChannelsPage from "./pages/Channels";
import ScanPage from "./pages/Scan";
import ScanProgressPage from "./pages/ScanProgress";
import ScanResultsPage from "./pages/ScanResults";
import SourceMappingPage from "./pages/SourceMapping";
import UpdatesPage from "./pages/Updates";
import VulnerabilitiesPage from "./pages/Vulnerabilities";
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
        <Route path="scan" element={<ScanPage />} />
        <Route path="scan/:requestId" element={<ScanProgressPage />} />
        <Route path="scans/:scanId" element={<ScanResultsPage />} />
        <Route path="vulnerabilities" element={<VulnerabilitiesPage />} />
        <Route path="updates" element={<UpdatesPage />} />
        <Route path="setup/sources" element={<SourceMappingPage />} />
        <Route path="setup/channels" element={<ChannelsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
