import { useEffect, useMemo, useState } from "react";
import type { Layer, NodeRecord } from "../types";
import type { AssetInboxItem, AssetInboxResponse } from "../types";
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
  assetInbox?: AssetInboxResponse | null;
  assetTreeSelection: AssetTreeSelection;
  assetStatusFilter: AssetStatusFilter;
  assetSearch: string;
  selectedAssetId: string;
  opsCount: number;
  reviewCount: number;
  assetCount: number;
  backlogCount: number;
  projectCount: number;
  loading: boolean;
  collapsed: boolean;
  onSelectNode(id: string): void;
  onAssetTreeSelectionChange(selection: AssetTreeSelection): void;
  onAssetStatusFilterChange(filter: AssetStatusFilter): void;
  onAssetSearchChange(query: string): void;
  onSelectAsset(assetId: string): void;
  onSelectView(v: ViewName): void;
  onToggleCollapsed(): void;
}

const FILTER_LAYERS: Layer[] = ["L1", "L2", "L3", "L4", "L7"];
type LayerFilter = Layer | "ALL";
export type AssetGroupId = "ALL" | "doc" | "test" | "config" | "source" | "generated" | "other";
export type AssetStatusFilter = "all" | "health" | "candidate" | "drift" | "orphan";
export interface AssetTreeSelection {
  groupId: AssetGroupId;
  bucketId: string;
}

const ASSET_STATUS_FILTERS: Array<{ id: AssetStatusFilter; label: string; title: string }> = [
  { id: "all", label: "All", title: "Show all asset states" },
  { id: "health", label: "Health", title: "Accepted, bound, healthy, or graph-current assets" },
  { id: "candidate", label: "Candidate", title: "Proposed, pending, or review-candidate assets" },
  { id: "drift", label: "Drift", title: "Suspected, confirmed, stale, or drifted assets" },
  { id: "orphan", label: "Orphan", title: "Unbound or actionable orphan assets" },
];

const LAYER_LABELS: Record<Layer, { label: string; title: string }> = {
  L1: { label: "Runtime", title: "L1 Runtime — project root / runtime boundary" },
  L2: { label: "Area", title: "L2 Area — product or source domain grouping" },
  L3: { label: "Subsystem", title: "L3 Subsystem — implementation subsystem" },
  L4: { label: "Asset", title: "L4 Asset — config / state / contract / artifact" },
  L7: { label: "Feature", title: "L7 Feature — inspectable implementation feature" },
};

const ASSET_GROUP_ORDER: AssetGroupId[] = ["ALL", "doc", "test", "config", "source", "generated", "other"];
const ASSET_GROUP_LABELS: Record<AssetGroupId, string> = {
  ALL: "All assets",
  doc: "Docs",
  test: "Tests",
  config: "Config",
  source: "Source",
  generated: "Generated / Ignored",
  other: "Other",
};

const ASSET_BUCKET_LABELS: Record<string, string> = {
  all: "All in group",
  health: "Health",
  candidate: "Candidate",
  drift: "Drift / stale",
  orphan: "Orphan / actionable",
  ignored: "Ignored / generated",
  unbound: "Doc unbound",
  accepted: "Accepted",
  pending: "Pending decision",
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
    assetInbox,
    assetTreeSelection,
    assetStatusFilter,
    assetSearch,
    selectedAssetId,
    opsCount,
    reviewCount,
    assetCount,
    backlogCount,
    projectCount,
    loading,
    collapsed,
  } = props;

  const idx = useMemo<Index>(() => buildIndex(nodes), [nodes]);
  const assetItems = useMemo(() => (assetInbox?.items ?? []).slice().sort(compareAssetRows), [assetInbox?.items]);
  const assetTree = useMemo(() => buildAssetTree(assetItems), [assetItems]);
  const visibleAssets = useMemo(
    () => filterAssetRows(assetItems, assetTreeSelection, assetStatusFilter, assetSearch),
    [assetItems, assetSearch, assetStatusFilter, assetTreeSelection],
  );

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
          {activeView === "assets" ? (
            <AssetTree
              loading={loading}
              total={assetItems.length}
              tree={assetTree}
              visibleAssets={visibleAssets}
              selection={assetTreeSelection}
              statusFilter={assetStatusFilter}
              search={assetSearch}
              selectedAssetId={selectedAssetId}
              onSelectionChange={props.onAssetTreeSelectionChange}
              onStatusFilterChange={props.onAssetStatusFilterChange}
              onSearchChange={props.onAssetSearchChange}
              onSelectAsset={props.onSelectAsset}
            />
          ) : (
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
        </>
      )}
    </aside>
  );
}

interface AssetTreeBucket {
  id: string;
  label: string;
  count: number;
  tone: "green" | "amber" | "red" | "gray";
}

interface AssetTreeGroup {
  id: AssetGroupId;
  label: string;
  count: number;
  tone: "green" | "amber" | "red" | "gray";
  buckets: AssetTreeBucket[];
}

function AssetTree(props: {
  loading: boolean;
  total: number;
  tree: AssetTreeGroup[];
  visibleAssets: AssetInboxItem[];
  selection: AssetTreeSelection;
  statusFilter: AssetStatusFilter;
  search: string;
  selectedAssetId: string;
  onSelectionChange(selection: AssetTreeSelection): void;
  onStatusFilterChange(filter: AssetStatusFilter): void;
  onSearchChange(query: string): void;
  onSelectAsset(assetId: string): void;
}) {
  return (
    <>
      <div className="sidebar-section-title" style={{ marginTop: 4 }}>
        Asset tree
      </div>
      <div className="tree-controls asset-tree-controls">
        <div className="tree-search-row">
          <input
            type="text"
            className="tree-search"
            placeholder="Search paths, nodes, evidence…"
            value={props.search}
            onChange={(event) => props.onSearchChange(event.target.value)}
          />
          {props.search ? (
            <button className="tree-search-clear" onClick={() => props.onSearchChange("")} title="Clear search">
              ×
            </button>
          ) : null}
        </div>
        <div className="tree-chip-row asset-status-filter-row">
          {ASSET_STATUS_FILTERS.map((filter) => (
            <button
              key={filter.id}
              className={`chip asset-status-chip ${props.statusFilter === filter.id ? "on" : "off"}`}
              onClick={() => props.onStatusFilterChange(filter.id)}
              title={filter.title}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <div className="tree-counter">
          {props.visibleAssets.length} of {props.total} assets
        </div>
      </div>
      <div className="sidebar-tree asset-sidebar-tree">
        {props.loading ? (
          <div className="asset-sidebar-empty">Loading…</div>
        ) : props.total === 0 ? (
          <div className="asset-sidebar-empty">No assets</div>
        ) : (
          props.tree.map((group) => (
            <div key={group.id}>
              <button
                type="button"
                className={`tree-row asset-tree-row ${props.selection.groupId === group.id && !props.selection.bucketId ? "active" : ""}`}
                onClick={() => props.onSelectionChange({ groupId: group.id, bucketId: "" })}
              >
                <span className={`asset-state-dot tone-${group.tone}`} />
                <span className="tree-name">{group.label}</span>
                <span className="tree-meta">{group.count}</span>
              </button>
              {group.buckets.map((bucket) => (
                <button
                  key={`${group.id}:${bucket.id}`}
                  type="button"
                  className={`tree-row asset-tree-row asset-tree-bucket ${
                    props.selection.groupId === group.id && props.selection.bucketId === bucket.id ? "active" : ""
                  }`}
                  onClick={() => props.onSelectionChange({ groupId: group.id, bucketId: bucket.id })}
                >
                  <span className={`asset-state-dot tone-${bucket.tone}`} />
                  <span className="tree-name">{bucket.label}</span>
                  <span className="tree-meta">{bucket.count}</span>
                </button>
              ))}
            </div>
          ))
        )}
        <div className="asset-sidebar-results">
          {props.visibleAssets.slice(0, 24).map((item) => (
            <button
              key={item.asset_id}
              type="button"
              className={`asset-sidebar-asset${props.selectedAssetId === item.asset_id ? " active" : ""}`}
              onClick={() => props.onSelectAsset(item.asset_id)}
              title={item.path}
            >
              <span className={`asset-state-dot tone-${assetTone(item)}`} />
              <span className="mono">{item.path}</span>
              <span>{assetStatusLabel(item.asset_status)}</span>
            </button>
          ))}
        </div>
      </div>
    </>
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

function buildAssetTree(items: AssetInboxItem[]): AssetTreeGroup[] {
  return ASSET_GROUP_ORDER.map((groupId) => {
    const groupItems = groupId === "ALL" ? items : items.filter((item) => assetGroupId(item) === groupId);
    const buckets = assetBucketsForGroup(groupId)
      .map((bucket) => {
        const bucketItems = groupItems.filter((item) => assetBucketMatches(item, bucket));
        return { id: bucket, label: ASSET_BUCKET_LABELS[bucket] ?? bucket, count: bucketItems.length, tone: bucketTone(bucket, bucketItems) };
      })
      .filter((bucket) => bucket.count > 0 || bucket.id === "all");
    return {
      id: groupId,
      label: ASSET_GROUP_LABELS[groupId],
      count: groupItems.length,
      tone: highestAssetTone(groupItems),
      buckets,
    };
  });
}

function assetBucketsForGroup(groupId: AssetGroupId): string[] {
  if (groupId === "doc") return ["all", "unbound", "candidate", "accepted", "orphan", "drift"];
  if (groupId === "generated") return ["all", "ignored"];
  if (groupId === "test" || groupId === "config") return ["all", "candidate", "accepted", "orphan", "drift", "pending"];
  return ["all", "health", "candidate", "orphan", "drift"];
}

function filterAssetRows(
  items: AssetInboxItem[],
  selection: AssetTreeSelection,
  statusFilter: AssetStatusFilter,
  search: string,
): AssetInboxItem[] {
  const q = search.trim().toLowerCase();
  return items.filter((item) => {
    if (selection.groupId !== "ALL" && assetGroupId(item) !== selection.groupId) return false;
    if (selection.bucketId && !assetBucketMatches(item, selection.bucketId)) return false;
    if (statusFilter !== "all" && !assetStatusFilterMatches(item, statusFilter)) return false;
    if (!q) return true;
    const relations = [
      ...(item.mount_relations ?? []),
      ...(item.accepted_bindings ?? []).map((binding) => ({
        target_node_id: binding.node_id,
        target_title: binding.title,
        status: "accepted",
        role: binding.role,
        source: binding.source,
        evidence_kind: "accepted_binding",
        proposal_hash: "",
      })),
      ...(item.binding_candidates ?? []).map((candidate) => ({
        target_node_id: candidate.target_node_id,
        target_title: candidate.target_title,
        status: "candidate",
        role: candidate.operation,
        source: candidate.source,
        evidence_kind: candidate.evidence_kind,
        proposal_hash: candidate.proposal_hash,
      })),
    ];
    const hay = [
      item.path,
      item.asset_kind,
      item.asset_status,
      item.graph_status,
      item.scan_status,
      item.binding_status,
      item.risk,
      ...(item.evidence ?? []).map((evidence) => `${evidence.kind} ${evidence.message}`),
      ...relations.map((relation) =>
        [
          relation.status,
          relation.role,
          relation.target_node_id,
          relation.target_title,
          relation.source,
          relation.evidence_kind,
          relation.proposal_hash,
        ].join(" "),
      ),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

function assetGroupId(item: AssetInboxItem): AssetGroupId {
  const kind = normalizeAssetKind(item.asset_kind);
  if (kind === "doc") return "doc";
  if (kind === "test") return "test";
  if (kind === "config") return "config";
  if (kind === "source") return "source";
  if (kind === "generated" || kind === "ignored" || item.asset_status === "ignored" || item.asset_status === "archive") return "generated";
  const lowerPath = item.path.toLowerCase();
  if (lowerPath.endsWith(".md") || lowerPath.endsWith(".mdx") || lowerPath.includes("/docs/")) return "doc";
  if (lowerPath.includes("/test") || lowerPath.endsWith(".test.ts") || lowerPath.endsWith(".spec.ts")) return "test";
  if (/\.(ya?ml|toml|ini|cfg|json)$/.test(lowerPath)) return "config";
  return "other";
}

function assetBucketMatches(item: AssetInboxItem, bucketId: string): boolean {
  if (!bucketId || bucketId === "all") return true;
  if (bucketId === "health" || bucketId === "accepted") return assetStatusFilterMatches(item, "health");
  if (bucketId === "candidate") return assetStatusFilterMatches(item, "candidate");
  if (bucketId === "drift") return assetStatusFilterMatches(item, "drift");
  if (bucketId === "orphan" || bucketId === "unbound") return assetStatusFilterMatches(item, "orphan");
  if (bucketId === "ignored") return item.asset_status === "ignored" || item.asset_status === "archive" || normalizeAssetKind(item.asset_kind) === "generated";
  if (bucketId === "pending") return item.asset_status.includes("pending") || item.asset_status.includes("decision");
  return item.asset_status === bucketId;
}

function assetStatusFilterMatches(item: AssetInboxItem, filter: AssetStatusFilter): boolean {
  const status = item.asset_status;
  if (filter === "health") {
    return status === "accepted" || item.binding_status === "accepted" || (item.accepted_bindings ?? []).length > 0 || item.graph_status === "current";
  }
  if (filter === "candidate") {
    return status.includes("candidate") || status.includes("pending") || (item.binding_candidates ?? []).length > 0;
  }
  if (filter === "drift") {
    return status.includes("drift") || status === "stale" || normalizeDriftState(item.drift?.state) !== "not_drifted";
  }
  if (filter === "orphan") {
    return status.includes("orphan") || status.includes("unbound") || item.scan_status === "orphan" || (item.accepted_bindings ?? []).length === 0;
  }
  return true;
}

function highestAssetTone(items: AssetInboxItem[]): "green" | "amber" | "red" | "gray" {
  if (items.some((item) => assetTone(item) === "red")) return "red";
  if (items.some((item) => assetTone(item) === "amber")) return "amber";
  if (items.some((item) => assetTone(item) === "green")) return "green";
  return "gray";
}

function bucketTone(bucket: string, items: AssetInboxItem[]): "green" | "amber" | "red" | "gray" {
  if (bucket === "accepted" || bucket === "health") return "green";
  if (bucket === "candidate" || bucket === "pending") return "amber";
  if (bucket === "drift" || bucket === "orphan" || bucket === "unbound") return "red";
  if (bucket === "ignored") return "gray";
  return highestAssetTone(items);
}

function assetTone(item: AssetInboxItem): "green" | "amber" | "red" | "gray" {
  const status = item.asset_status;
  if (status === "accepted" || status === "drift_resolved" || status === "drift_waived") return "green";
  if (status.includes("candidate") || status.includes("pending") || status === "drift_suspected") return "amber";
  if (status.includes("orphan") || status.includes("unbound") || status === "drift_confirmed" || status === "stale") return "red";
  if (status === "ignored" || status === "archive" || normalizeAssetKind(item.asset_kind) === "generated") return "gray";
  return "gray";
}

function assetStatusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

function normalizeDriftState(state?: string): string {
  return state === "suspected" || state === "confirmed" || state === "resolved" || state === "waived" ? state : "not_drifted";
}

function normalizeAssetKind(kind?: string): string {
  const value = (kind || "").trim().toLowerCase();
  if (value === "index_doc") return "doc";
  if (value === "unknown") return "other";
  return value;
}

function compareAssetRows(a: AssetInboxItem, b: AssetInboxItem): number {
  const group = ASSET_GROUP_ORDER.indexOf(assetGroupId(a)) - ASSET_GROUP_ORDER.indexOf(assetGroupId(b));
  if (group !== 0) return group;
  return a.path.localeCompare(b.path);
}
