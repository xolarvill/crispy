import { useEffect, useState } from "react";
import { RefreshCw, Search, ChevronRight, ArrowLeft } from "lucide-react";
import { api } from "./data";
import { demoShops, type Shop } from "./shop-data";
import { Button } from "./components/ui/button";
import { IconButton } from "./components/console-controls";
import "./operations.css";
type Row = {
  id: string;
  title?: string;
  shop_name?: string;
  task_type?: string;
  status?: string;
  state?: string;
  memory_type?: string;
  memory_scope?: string;
  source_type?: string;
  scheduled_date?: string;
  scheduled_time?: string;
  channel?: string;
  created_at?: string;
  content?: Record<string, unknown>;
  error_message?: string;
};
const titles: Record<string, string> = {
  "product-research": "产品研究",
  "competitor-research": "竞品研究",
  memory: "记忆库",
  calendar: "发布排期",
  data: "数据",
};
const demoRows: Record<string, Row[]> = {
  research: [
    {
      id: "r1",
      shop_name: "Atelier Studio",
      task_type: "store_context",
      status: "completed",
      created_at: "2026-11-01",
      content: { summary: "店铺研究演示记录", evidence: [] },
    },
    {
      id: "r2",
      shop_name: "Northline",
      task_type: "competitive_landscape",
      status: "queued",
      created_at: "2026-11-01",
    },
  ],
  memory: [
    {
      id: "m1",
      memory_type: "shop_profile",
      memory_scope: "shop",
      source_type: "research_intelligence",
      status: "active",
      content: {
        summary: "自然光与面料细节作为产品表达重点。此条为演示记忆。",
      },
    },
  ],
  calendar: [
    {
      id: "s1",
      title: "秋季系列 · 产品图文",
      channel: "instagram",
      scheduled_date: "2026-11-03",
      scheduled_time: "10:00",
      state: "draft",
    },
    {
      id: "s2",
      title: "新品短视频",
      channel: "tiktok",
      scheduled_date: "2026-11-05",
      scheduled_time: "18:00",
      state: "scheduled",
    },
  ],
};
const labels: Record<string, string> = {
  completed: "已完成",
  queued: "排队中",
  running: "进行中",
  failed: "失败",
  active: "有效",
  draft: "草稿",
  scheduled: "已排期",
  published: "已发布",
  store_context: "店铺研究",
  full_intelligence: "综合研究",
  industry_baseline: "行业研究",
  audience_pain_points: "受众研究",
  compliance_scan: "合规研究",
  competitive_landscape: "竞品研究",
  shop_profile: "店铺画像",
};
function label(value?: string) {
  return value ? labels[value] || value : "—";
}
export default function OperationsPage({
  section,
  demo,
}: {
  section: string;
  demo: boolean;
}) {
  const [shops, setShops] = useState<Shop[]>(demo ? demoShops : []),
    [shop, setShop] = useState(demo ? demoShops[0].id : ""),
    [rows, setRows] = useState<Row[]>([]),
    [metrics, setMetrics] = useState<Record<string, unknown> | null>(null),
    [selected, setSelected] = useState<Row | null>(null),
    [query, setQuery] = useState(""),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [revision, setRevision] = useState(0),
    [project, setProject] = useState(""),
    [submittedProject, setSubmittedProject] = useState("");
  const [start, setStart] = useState(
      () =>
        new Date()
          .toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" })
          .slice(0, 7) + "-01",
    ),
    [end, setEnd] = useState(() => {
      const now = new Date();
      return new Date(
        now.getFullYear(),
        now.getMonth() + 1,
        0,
      ).toLocaleDateString("sv-SE");
    });
  useEffect(() => {
    if (demo) return;
    const c = new AbortController();
    api<{ shops: Shop[] }>("/shops?limit=50", { signal: c.signal })
      .then((r) => {
        setShops(r.shops);
        setShop(r.shops[0]?.id || "");
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => c.abort();
  }, [demo]);
  useEffect(() => {
    setLoading(false);
    setSelected(null);
    setError("");
    setMetrics(null);
    setRows([]);
    if (demo) {
      setRows(
        demoRows[section.endsWith("-research") ? "research" : section] || [],
      );
      if (section === "data")
        setMetrics({
          product_count: 18,
          shopify_revenue: 28460,
          meta_spend: 3800,
          overall_roas: 3.4,
          total_impressions: 124800,
          total_clicks: 4610,
        });
      return;
    }
    if (!shop && section !== "memory") return;
    if (section === "data" && !submittedProject) return;
    if (section === "calendar" && (!start || !end || start > end)) {
      setError("请选择有效日期范围");
      return;
    }
    const c = new AbortController();
    setLoading(true);
    const params = new URLSearchParams();
    let url = "";
    if (section === "memory") url = "/gm-memory?limit=100";
    if (section.endsWith("-research"))
      url =
        "/shop-analysis/tasks?limit=100&shop_id=" + encodeURIComponent(shop);
    if (section === "calendar") {
      params.set("workspace_id", shop);
      params.set("start_date", start);
      params.set("end_date", end);
      url = "/content-schedules?" + params;
    }
    if (section === "data") {
      params.set(
        "workspace_name",
        shops.find((s) => s.id === shop)?.name || "",
      );
      params.set("project_name", submittedProject);
      url = "/data-dashboard/summary?" + params;
    }
    api<Row[] | { items: Row[] } | Record<string, unknown>>(url, {
      signal: c.signal,
    })
      .then((r) => {
        if (section === "data") setMetrics(r as Record<string, unknown>);
        else setRows(Array.isArray(r) ? r : (r as { items: Row[] }).items);
      })
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!c.signal.aborted) setLoading(false);
      });
    return () => c.abort();
  }, [demo, section, shop, revision, start, end, submittedProject, shops]);
  const filtered = rows.filter(
    (r) =>
      (section === "competitor-research"
        ? r.task_type === "competitive_landscape"
        : section === "product-research"
          ? r.task_type !== "competitive_landscape"
          : true) &&
      JSON.stringify(r).toLowerCase().includes(query.toLowerCase()),
  );
  return (
    <div className="operations-page">
      <div className="resource-header">
        <p className="page-title" tabIndex={-1}>
          {titles[section]}
        </p>
        <IconButton
          icon={RefreshCw}
          label="刷新"
          onClick={() => setRevision((v) => v + 1)}
        />
      </div>
      <div className="operations-toolbar">
        {section !== "memory" && (
          <label>
            店铺
            <select
              value={shop}
              onChange={(e) => {
                setShop(e.target.value);
                setSubmittedProject("");
              }}
            >
              <option value="" disabled>
                选择店铺
              </option>
              {shops.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        )}
        {section === "calendar" && (
          <>
            <label>
              从
              <input
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              至
              <input
                type="date"
                value={end}
                onChange={(e) => setEnd(e.target.value)}
              />
            </label>
          </>
        )}
        {section === "data" && (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setSubmittedProject(project.trim());
              setRevision((v) => v + 1);
            }}
          >
            <label>
              项目
              <input
                required
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder="项目名称"
              />
            </label>
            <Button variant="outline" type="submit">
              查看
            </Button>
          </form>
        )}
        {section !== "data" && (
          <label className="operations-search">
            <Search size={15} />
            <input
              aria-label="搜索记录"
              placeholder="搜索"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        )}
      </div>
      {(demo || section === "memory") && (
        <p className="muted operations-disclosure">
          {demo ? "演示数据" : "全部店铺 · 有效记忆"}
        </p>
      )}
      {error && (
        <div role="alert" className="alert">
          {error}
          <Button variant="outline" onClick={() => setRevision((v) => v + 1)}>
            重试
          </Button>
        </div>
      )}
      {loading ? (
        <p role="status" className="empty-state">
          加载中…
        </p>
      ) : selected ? (
        <section className="operations-detail">
          <Button variant="ghost" onClick={() => setSelected(null)}>
            <ArrowLeft size={15} />
            返回列表
          </Button>
          <p className="operations-detail-title">
            {selected.title ||
              selected.shop_name ||
              label(selected.memory_type)}
          </p>
          <dl>
            {Object.entries(selected)
              .filter(([k]) => k !== "content" && k !== "id")
              .map(([k, v]) => (
                <div key={k}>
                  <dt>
                    {(
                      {
                        title: "名称",
                        shop_name: "店铺",
                        task_type: "研究类型",
                        status: "状态",
                        state: "状态",
                        memory_type: "记忆类型",
                        memory_scope: "作用域",
                        source_type: "来源",
                        scheduled_date: "日期",
                        scheduled_time: "时间",
                        channel: "渠道",
                        created_at: "创建时间",
                        error_message: "错误",
                      } as Record<string, string>
                    )[k] || k}
                  </dt>
                  <dd>
                    {typeof v === "object"
                      ? JSON.stringify(v)
                      : label(String(v ?? "—"))}
                  </dd>
                </div>
              ))}
          </dl>
          {selected.content && (
            <pre>{JSON.stringify(selected.content, null, 2)}</pre>
          )}
        </section>
      ) : section === "data" ? (
        <div>
          {metrics ? (
            <dl className="operations-metrics">
              {[
                ["product_count", "产品"],
                ["shopify_revenue", "店铺收入"],
                ["meta_spend", "广告花费"],
                ["overall_roas", "ROAS"],
                ["total_impressions", "曝光"],
                ["total_clicks", "点击"],
              ].map(([id, name]) => (
                <div key={id}>
                  <dt>{name}</dt>
                  <dd>
                    {metrics[id] === undefined
                      ? "—"
                      : Number(metrics[id]).toLocaleString("zh-CN")}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            !error && <p className="empty-state">选择店铺并输入项目名称</p>
          )}
        </div>
      ) : (
        <div className="operations-list">
          {filtered.length
            ? filtered.map((r) => (
                <button
                  className="operations-row"
                  key={r.id}
                  onClick={() => setSelected(r)}
                >
                  <span>
                    <strong>
                      {r.title || r.shop_name || label(r.memory_type)}
                    </strong>
                    <small>
                      {label(r.channel || r.task_type || r.memory_scope)}
                    </small>
                  </span>
                  <span>{label(r.state || r.status)}</span>
                  <time>
                    {r.scheduled_date || r.created_at?.slice(0, 10)}{" "}
                    {r.scheduled_time}
                  </time>
                  <ChevronRight size={16} />
                </button>
              ))
            : !error && (
                <p className="empty-state">
                  {query ? "没有匹配记录" : "暂无记录"}
                </p>
              )}
        </div>
      )}
    </div>
  );
}
