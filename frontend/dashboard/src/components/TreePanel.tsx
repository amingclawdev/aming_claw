import { useEffect, useMemo, useState } from "react";
import type { Layer, NodeRecord } from "../types";
import {
  aggregateNode,
  classifyNode,
  isPackageMarker,
  newSubtreeAggregate,
  semStatusDotClass,
  semStatusLabel,
  type SubtreeAggregate,
} from "../lib/semantic";
import { healthHex } from "../lib/health";
import type { ViewName } from "../App";

interface Props {
  nodes: NodeRecord[];
  selectedNodeId: string | null;
  activeView: ViewName;
  opsCount: number;
  reviewCount: number;
  assetCount: number;
  backlogCount: number;
  projectCount: number;
  loading: boolean;
  collapsed: boolean;
  onSelectNode(id: string): void;
  onSelectView(v: ViewName): void;
  onToggleCollapsed(): void;
}

const FILTER_LAYERS: Layer[] = ["L1", "L2", "L3", "L4", "L7"];
type LayerFilter = Layer | "ALL";

const LAYER_LABELS: Record<Layer, { label: string; title: string }> = {
  L1: { label: "Runtime", title: "L1 Runtime — project root / runtime boundary" },
  L2: { label: "Area", title: "L2 Area — product or source domain grouping" },
  L3: { label: "Subsystem", title: "L3 Subsystem — implementation subsystem" },
  L4: { label: "Asset", title: "L4 Asset — config / state / contract / artifact" },
  L7: { label: "Feature", title: "L7 Feature — inspectable implementation feature" },
};

interface Index {
  byId: Map<string, NodeRecord>;
  byParent: Map<string, NodeRecord[]>;
  roots: NodeRecord[];
  agg: Map<string, SubtreeAggregate>;
}

export default function TreePanel(props: Props) {
  const {
    nodes,
    selectedNodeId,
    activeView,
    opsCount,
    reviewCount,
    assetCount,
    backlogCount,
    projectCount,
    loading,
    collapsed,
  } = props;

  const idx = useMemo<Index>(() => buildIndex(nodes), [nodes]);

  const [search, setSearch] = useState("");
  const [layerFilter, setLayerFilter] = useState<LayerFilter>("ALL");

  // Compute the set of node_ids that should be visible given the search +
  // layer filters. Search-only mode includes ancestors so paths stay
  // walkable; layer mode intentionally returns only that semantic layer.
  const visible = useMemo<Set<string> | null>(() => {
    const q = search.trim().toLowerCase();
    const activeLayer = layerFilter === "ALL" ? null : layerFilter;
    if (!q && !activeLayer) return null; // unfiltered
    const matches = new Set<string>();
    nodes.forEach((n) => {
      if (activeLayer && n.layer !== activeLayer) return;
      if (q) {
        const hay = `${n.node_id} ${n.title || ""} ${(n.primary_files || []).join(" ")} ${(n.metadata?.module || "")}`.toLowerCase();
        if (!hay.includes(q)) return;
      }
      matches.add(n.node_id);
      if (activeLayer) return;
      // Include ancestors so path is reachable.
      let cur: NodeRecord | undefined = n;
      let safety = 8;
      while (cur && safety-- > 0) {
        const parent = cur.metadata?.hierarchy_parent;
        if (!parent) break;
        matches.add(parent);
        cur = idx.byId.get(parent);
      }
    });
    return matches;
  }, [nodes, idx.byId, search, layerFilter]);

  const treeRoots = useMemo(() => {
    if (visible && layerFilter !== "ALL") {
      return sortChildren(nodes.filter((n) => visible.has(n.node_id)));
    }
    return idx.roots.filter((r) => !visible || visible.has(r.node_id));
  }, [idx.roots, layerFilter, nodes, visible]);

  // Default: expand L1 roots only.
  const [expanded, setExpanded] = useState<Set<string>>(() => {
    const init = new Set<string>();
    nodes.forEach((n) => {
      if (n.layer === "L1") init.add(n.node_id);
    });
    return init;
  });

  // While a non-trivial filter is active, expand all visible ancestors so the
  // matches are revealed. When the filter clears, leave expansion untouched.
  useEffect(() => {
    if (!visible) return;
    setExpanded((prev) => {
      const next = new Set(prev);
      visible.forEach((id) => next.add(id));
      return next;
    });
  }, [visible]);

  // When the data set changes, keep root nodes expanded so the tree never
  // collapses entirely on refresh.
  useEffect(() => {
    setExpanded((prev) => {
      const next = new Set(prev);
      idx.roots.forEach((r) => next.add(r.node_id));
      return next;
    });
  }, [idx.roots]);

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <aside className={`sidebar${collapsed ? " collapsed" : ""} scrollbar-thin`}>
      <div className="sidebar-collapse-head">
        {collapsed ? null : <span className="sidebar-section-title inline">Views</span>}
        <button
          type="button"
          className="icon-btn sidebar-collapse-btn"
          onClick={props.onToggleCollapsed}
          aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          title={collapsed ? "Expand navigation" : "Collapse navigation"}
        >
          {collapsed ? "›" : "‹"}
        </button>
      </div>
      <div className="sidebar-section">
        <NavRow
          icon="▣"
          label="Projects"
          meta={loading ? "…" : String(projectCount)}
          active={activeView === "projects"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("projects")}
        />
        <NavRow
          icon="▦"
          label="Project overview"
          meta={`${nodes.length}`}
          active={activeView === "overview"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("overview")}
        />
        <NavRow
          icon="◉"
          label="Graph"
          meta={loading ? "…" : `${nodes.length}`}
          active={activeView === "graph"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("graph")}
        />
        <NavRow
          icon="⚙"
          label="Operations Queue"
          meta={loading ? "…" : String(opsCount)}
          active={activeView === "operations"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("operations")}
        />
        <NavRow
          icon="⚖"
          label="Review Queue"
          meta={loading ? "…" : String(reviewCount)}
          active={activeView === "review"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("review")}
        />
        <NavRow
          icon="□"
          label="Asset Inbox"
          meta={loading ? "…" : String(assetCount)}
          active={activeView === "assets"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("assets")}
        />
        <NavRow
          icon="▤"
          label="Backlog"
          meta={loading ? "…" : String(backlogCount)}
          active={activeView === "backlog"}
          collapsed={collapsed}
          onClick={() => props.onSelectView("backlog")}
        />
      </div>

      {collapsed ? null : (
        <>
          <div className="sidebar-section-title" style={{ marginTop: 4 }}>
            Graph tree
          </div>
          <div className="tree-controls">
            <div className="tree-search-row">
              <input
                type="text"
                className="tree-search"
                placeholder="Search nodes / files…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              {search ? (
                <button className="tree-search-clear" onClick={() => setSearch("")} title="Clear search">
                  ×
                </button>
              ) : null}
            </div>
            <div className="tree-chip-row">
              <button
                className={`chip layer-chip${layerFilter === "ALL" ? " on" : " off"}`}
                onClick={() => setLayerFilter("ALL")}
                title="Show all layers"
              >
                All
              </button>
              {FILTER_LAYERS.map((l) => {
                const meta = LAYER_LABELS[l];
                return (
                  <button
                    key={l}
                    className={`chip layer-chip layer-${l}${layerFilter === l ? " on" : " off"}`}
                    onClick={() => setLayerFilter(l)}
                    title={`${meta.title}. Click to show only ${l} nodes.`}
                  >
                    <span className="layer-chip-code">{l}</span>
                    <span className="layer-chip-label">{meta.label}</span>
                  </button>
                );
              })}
            </div>
            <div className="tree-counter">
              {visible == null ? `${nodes.length} nodes` : `${visible.size} of ${nodes.length} nodes`}
            </div>
          </div>
          <div className="sidebar-tree">
            {idx.roots.length === 0 || treeRoots.length === 0 ? (
              <div style={{ fontSize: 11, color: "var(--ink-500)", padding: "10px 12px" }}>
                {loading ? "Loading…" : layerFilter === "ALL" ? "No nodes" : `No ${layerFilter} nodes`}
              </div>
            ) : (
              treeRoots
                .map((root) => (
                  <TreeNode
                    key={root.node_id}
                    node={root}
                    depth={0}
                    idx={idx}
                    visible={visible}
                    expanded={expanded}
                    selectedNodeId={selectedNodeId}
                    onToggle={toggle}
                    onSelectNode={props.onSelectNode}
                  />
                ))
            )}
          </div>
        </>
      )}
    </aside>
  );
}

function NavRow(props: {
  icon: string;
  label: string;
  meta: string;
  active: boolean;
  collapsed: boolean;
  onClick(): void;
}) {
  return (
    <div
      className={`tree-row nav-row${props.active ? " active" : ""}${props.collapsed ? " collapsed" : ""}`}
      onClick={props.onClick}
      title={props.collapsed ? `${props.label} ${props.meta}` : undefined}
      style={{ padding: "5px 8px" }}
    >
      <span className="tree-icon">{props.icon}</span>
      {props.collapsed ? null : (
        <>
          <span className="tree-name" style={{ fontWeight: 600 }}>
            {props.label}
          </span>
          <span className="tree-meta">{props.meta}</span>
        </>
      )}
    </div>
  );
}

function TreeNode(props: {
  node: NodeRecord;
  depth: number;
  idx: Index;
  visible: Set<string> | null;
  expanded: Set<string>;
  selectedNodeId: string | null;
  onToggle(id: string): void;
  onSelectNode(id: string): void;
}) {
  const { node, depth, idx, visible, expanded, selectedNodeId } = props;
  const allChildren = idx.byParent.get(node.node_id) ?? [];
  const children = visible ? allChildren.filter((c) => visible.has(c.node_id)) : allChildren;
  const isLeaf = children.length === 0;
  const isOpen = expanded.has(node.node_id);
  const isActive = selectedNodeId === node.node_id;

  const onClick = () => {
    if (!isLeaf) props.onToggle(node.node_id);
    props.onSelectNode(node.node_id);
  };

  return (
    <div>
      <div
        className={`tree-row${isActive ? " active" : ""}`}
        onClick={onClick}
        style={{ paddingLeft: 6 + depth * 14 }}
        title={node.title}
      >
        <span className={`tree-caret${isOpen ? " open" : ""}${isLeaf ? " leaf" : ""}`}>▶</span>
        <span className={`layer-badge layer-${node.layer}`}>{node.layer}</span>
        <span className="tree-name">{node.title || node.node_id}</span>
        <RowMeta node={node} idx={idx} />
      </div>
      {!isLeaf && isOpen ? (
        <div>
          {sortChildren(children).map((c) => (
            <TreeNode
              key={c.node_id}
              node={c}
              depth={depth + 1}
              idx={idx}
              visible={visible}
              expanded={expanded}
              selectedNodeId={selectedNodeId}
              onToggle={props.onToggle}
              onSelectNode={props.onSelectNode}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function RowMeta({ node, idx }: { node: NodeRecord; idx: Index }) {
  const isLeaf = !idx.byParent.has(node.node_id);
  if (node.layer === "L7" && isLeaf) {
    if (isPackageMarker(node)) {
      return <span className="tree-meta" title="Package marker — excluded from feature scoring">pkg</span>;
    }
    const status = classifyNode(node);
    const h = node._health;
    return (
      <span
        className="tree-meta"
        title={`semantic: ${semStatusLabel(status)}\nhealth: ${h ?? "—"}`}
      >
        <span className={`sem-dot ${semStatusDotClass(status)}`} />
        <span
          className="pdot"
          style={{ background: healthHex(h), marginLeft: 4 }}
          title={`health score: ${h ?? "—"}`}
        />
        <span
          className="mono"
          style={{
            fontSize: 10,
            fontWeight: 600,
            color: healthHex(h),
            marginLeft: 3,
          }}
        >
          {h != null ? h : "—"}
        </span>
      </span>
    );
  }
  // Container row — render aggregated semantic chip plus subtree size hint.
  const a = idx.agg.get(node.node_id);
  if (!a || a.total === 0) {
    // L4 leaves are unscored — render a plain "asset" label, no number,
    // no health dot. asset_binding got retired (no health concept for L4).
    if (node.layer === "L4") {
      return (
        <span
          className="tree-meta"
          title="L4 asset — config / state / contract / artifact (unscored)"
        >
          <span style={{ color: "var(--ink-400)", fontSize: 10 }}>asset</span>
        </span>
      );
    }
    return null;
  }
  const cur = a.complete + a.reviewed;
  const total = a.total;
  const curClass = cur === total ? "full" : cur === 0 ? "empty" : "";
  const parts: React.ReactNode[] = [];
  // Container health rollup (recursive avg of L7 descendants). Lead with the
  // dot so the operator scans the tree by color before reading numbers.
  if (node._health != null) {
    parts.push(
      <span
        key="h"
        title={`feature health: ${node._health}`}
        style={{ display: "inline-flex", alignItems: "center", gap: 3, marginRight: 4 }}
      >
        <span className="pdot" style={{ background: healthHex(node._health) }} />
        <span
          className="mono"
          style={{ fontSize: 10, fontWeight: 600, color: healthHex(node._health) }}
        >
          {node._health}
        </span>
      </span>,
    );
  }
  parts.push(
    <span key="cur" className={`num-cur ${curClass}`} title={`${cur} current of ${total} governed L7`}>
      {cur}/{total}
    </span>,
  );
  if (a.stale > 0) {
    parts.push(
      <span key="S" className="marker-S" title={`${a.stale} stale`}>
        {a.stale}S
      </span>,
    );
  }
  if (a.hash_unverified > 0) {
    parts.push(
      <span key="D" className="marker-D" title={`${a.hash_unverified} hash-unverified`}>
        {a.hash_unverified}D
      </span>,
    );
  }
  if (a.struct > 0) {
    parts.push(
      <span key="M" className="marker-M" title={`${a.struct} structure-only / missing semantic`}>
        {a.struct}M
      </span>,
    );
  }
  const pend = a.pending + a.running;
  if (pend > 0) {
    parts.push(
      <span key="P" className="marker-P" title={`${pend} pending or running`}>
        {pend}P
      </span>,
    );
  }
  if (a.review > 0) {
    parts.push(
      <span key="R" className="marker-R" title={`${a.review} review pending`}>
        {a.review}R
      </span>,
    );
  }
  return (
    <span className="tree-meta">
      {parts.map((p, i) => (
        <span key={i} style={{ display: "inline-flex", alignItems: "center" }}>
          {p}
        </span>
      ))}
    </span>
  );
}

function buildIndex(nodes: NodeRecord[]): Index {
  const byId = new Map<string, NodeRecord>();
  const byParent = new Map<string, NodeRecord[]>();
  nodes.forEach((n) => byId.set(n.node_id, n));
  nodes.forEach((n) => {
    const p = n.metadata?.hierarchy_parent;
    if (p && byId.has(p)) {
      const arr = byParent.get(p) ?? [];
      arr.push(n);
      byParent.set(p, arr);
    }
  });
  const roots: NodeRecord[] = [];
  nodes.forEach((n) => {
    const p = n.metadata?.hierarchy_parent;
    if (!p || !byId.has(p)) roots.push(n);
  });
  // Stable ordering: L1 first, then by node_id.
  roots.sort(layerThenId);

  const agg = new Map<string, SubtreeAggregate>();
  function walk(id: string): SubtreeAggregate {
    const cached = agg.get(id);
    if (cached) return cached;
    const node = byId.get(id);
    const out = newSubtreeAggregate();
    if (!node) {
      agg.set(id, out);
      return out;
    }
    const kids = byParent.get(id) ?? [];
    if (kids.length === 0) {
      aggregateNode(out, node);
      agg.set(id, out);
      return out;
    }
    kids.forEach((k) => mergeAgg(out, walk(k.node_id)));
    agg.set(id, out);
    return out;
  }
  roots.forEach((r) => walk(r.node_id));

  return { byId, byParent, roots, agg };
}

function mergeAgg(into: SubtreeAggregate, from: SubtreeAggregate) {
  into.total += from.total;
  into.complete += from.complete;
  into.reviewed += from.reviewed;
  into.hash_unverified += from.hash_unverified;
  into.pending += from.pending;
  into.running += from.running;
  into.stale += from.stale;
  into.failed += from.failed;
  into.review += from.review;
  into.struct += from.struct;
}

function layerThenId(a: NodeRecord, b: NodeRecord): number {
  const la = layerOrder(a.layer as Layer);
  const lb = layerOrder(b.layer as Layer);
  if (la !== lb) return la - lb;
  return a.node_id.localeCompare(b.node_id, "en", { numeric: true });
}

function layerOrder(l: Layer | string): number {
  switch (l) {
    case "L1":
      return 1;
    case "L2":
      return 2;
    case "L3":
      return 3;
    case "L4":
      return 4;
    case "L7":
      return 7;
    default:
      return 99;
  }
}

function sortChildren(children: NodeRecord[]): NodeRecord[] {
  return children.slice().sort(layerThenId);
}
