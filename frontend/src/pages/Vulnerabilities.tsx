import { useVulns } from "../api/hooks";
import { Async } from "../components/ui";
import { VulnTable } from "../components/VulnTable";

export default function VulnerabilitiesPage() {
  const query = useVulns("all");
  return (
    <>
      <h1>Vulnerabilities</h1>
      <p className="lede">Known advisories in every package of the latest scan of each repository, with whether your code reaches them.</p>
      <Async query={query} what="vulnerabilities">
        {(data) => <VulnTable data={data} showRepo />}
      </Async>
    </>
  );
}
