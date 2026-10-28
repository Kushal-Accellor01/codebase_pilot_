import { useMemo } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Controls, ReactFlow, type Edge, type Node } from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import type { Graph } from "../api/models";

const WIDTH = 210;
const HEIGHT = 40;

/** Left-to-right layout: the package on the left, files that import it, then files they import. */
export function layout(graph: Graph): { nodes: Node[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 14, ranksep: 70 });
  g.setDefaultEdgeLabel(() => ({}));
  graph.nodes.forEach((n) => g.setNode(n.id, { width: WIDTH, height: HEIGHT }));
  // A file "resolves to" the package it imports; for reading, the package sits at the left, so flip that edge.
  graph.edges.forEach((e) => (e.kind === "resolves_to" ? g.setEdge(e.target, e.source) : g.setEdge(e.source, e.target)));
  dagre.layout(g);

  const nodes: Node[] = graph.nodes.map((n) => {
    const p = g.node(n.id);
    const suffix = n.findings ? ` · ${n.findings}` : "";
    return {
      id: n.id,
      position: { x: p.x - WIDTH / 2, y: p.y - HEIGHT / 2 },
      data: { label: `${n.label}${suffix}` },
      className: `rf-${n.status}`,
      style: { width: WIDTH },
      ariaLabel: `${n.kind} ${n.label}, ${n.status.replace("_", " ")}${n.findings ? `, ${n.findings} affected lines` : ""}`,
    };
  });
  const edges: Edge[] = graph.edges.map((e, i) => ({
    id: `${e.source}->${e.target}-${i}`,
    source: e.source,
    target: e.target,
    animated: e.kind === "resolves_to",
    style: e.kind === "owns" ? { strokeDasharray: "4 3" } : undefined,
  }));
  return { nodes, edges };
}

export function BlastRadius({ graph, onSelect }: { graph: Graph; onSelect: (path: string) => void }) {
  const { nodes, edges } = useMemo(() => layout(graph), [graph]);
  const affected = graph.nodes.filter((n) => n.status === "affected").length;
  const radius = graph.nodes.filter((n) => n.status === "blast_radius").length;
  return (
    <>
      <p className="small muted" style={{ marginTop: 0 }}>
        {affected} {affected === 1 ? "file has" : "files have"} lines that change; {radius} more{" "}
        {radius === 1 ? "file depends" : "files depend"} on them and could be affected indirectly. Click a file to see its lines.
        {graph.capped ? " Showing the most affected files only." : ""}
      </p>
      <div className="legendrow" aria-hidden="true">
        <span><span className="dot" style={{ background: "var(--medium)" }} /> Package</span>
        <span><span className="dot" style={{ background: "var(--critical)" }} /> Affected lines</span>
        <span><span className="dot" style={{ background: "var(--high)" }} /> Depends on affected code</span>
        <span><span className="dot" style={{ background: "var(--rule)" }} /> Team</span>
      </div>
      <div className="graphbox" data-testid="blast-radius">
        <ReactFlow
          nodes={nodes} edges={edges} fitView nodesDraggable={false} nodesConnectable={false} minZoom={0.3}
          onNodeClick={(_, node) => node.id.startsWith("file:") && onSelect(node.id.slice(5))}
          proOptions={{ hideAttribution: true }}
        >
          <Background />
          <Controls showInteractive={false} />
        </ReactFlow>
      </div>
      <ul className="sr-only">
        {graph.nodes.filter((n) => n.kind === "file").map((n) => (
          <li key={n.id}>{n.label}: {n.status.replace("_", " ")}{n.findings ? `, ${n.findings} affected lines` : ""}</li>
        ))}
      </ul>
    </>
  );
}
