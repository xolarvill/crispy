import { useEffect, useState } from "react";
import { RefreshCw, Search, ChevronRight, ArrowLeft } from "lucide-react";
import { api } from "./data";
import { demoShops, type Shop } from "./shop-data";
import { pick, useI18n, type Localized, type Locale } from "./i18n";
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
const titles: Record<string, Localized> = {
  "product-research": { en: "Product research", zh: "产品研究" },
  "competitor-research": { en: "Competitor research", zh: "竞品研究" },
  memory: { en: "Memory", zh: "记忆库" },
  calendar: { en: "Publishing calendar", zh: "发布排期" },
  data: { en: "Data", zh: "数据" },
};
function demoRows(locale: Locale): Record<string, Row[]> {
  const name = (en: string, zh: string) => pick({ en, zh }, locale);
  return {
    research: [
      {
        id: "r1",
        shop_name: "Atelier Studio",
        task_type: "store_context",
        status: "completed",
        created_at: "2026-11-01",
        content: {
          summary: name("Demo store research record", "店铺研究演示记录"),
          evidence: [],
        },
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
          summary: name(
            "Natural light and fabric detail lead the product expression. This is a demo memory.",
            "自然光与面料细节作为产品表达重点。此条为演示记忆。",
          ),
        },
      },
    ],
    calendar: [
      {
        id: "s1",
        title: name("Autumn series · product story", "秋季系列 · 产品图文"),
        channel: "instagram",
        scheduled_date: "2026-11-03",
        scheduled_time: "10:00",
        state: "draft",
      },
      {
        id: "s2",
        title: name("New product short video", "新品短视频"),
        channel: "tiktok",
        scheduled_date: "2026-11-05",
        scheduled_time: "18:00",
        state: "scheduled",
      },
    ],
  };
}
const labels: Record<string, Localized> = {
  completed: { en: "Completed", zh: "已完成" },
  queued: { en: "Queued", zh: "排队中" },
  running: { en: "Running", zh: "进行中" },
  failed: { en: "Failed", zh: "失败" },
  active: { en: "Active", zh: "有效" },
  draft: { en: "Draft", zh: "草稿" },
  scheduled: { en: "Scheduled", zh: "已排期" },
  published: { en: "Published", zh: "已发布" },
  store_context: { en: "Store research", zh: "店铺研究" },
  full_intelligence: { en: "Full research", zh: "综合研究" },
  industry_baseline: { en: "Industry research", zh: "行业研究" },
  audience_pain_points: { en: "Audience research", zh: "受众研究" },
  compliance_scan: { en: "Compliance research", zh: "合规研究" },
  competitive_landscape: { en: "Competitor research", zh: "竞品研究" },
  shop_profile: { en: "Shop profile", zh: "店铺画像" },
  store_profile: { en: "Store profile", zh: "店铺画像" },
  "product-research": { en: "Product research", zh: "产品研究" },
  "competitor-research": { en: "Competitor research", zh: "竞品研究" },
  memory: { en: "Memory", zh: "记忆库" },
  calendar: { en: "Calendar", zh: "发布排期" },
  data: { en: "Data", zh: "数据" },
};
function label(value: string | undefined, locale: Locale) {
  const entry = value ? labels[value] : undefined;
  return entry ? pick(entry, locale) : value || "—";
}
export default function OperationsPage({
  section,
  demo,
}: {
  section: string;
  demo: boolean;
}) {
  const { t, locale } = useI18n();
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
  const [researchUrl, setResearchUrl] = useState("");
  const [researchFocus, setResearchFocus] = useState("full_intelligence");
  const [dispatching, setDispatching] = useState(false);
  const [dispatchError, setDispatchError] = useState("");
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
    setResearchUrl(shops.find((s) => s.id === shop)?.store_url || "");
  }, [shops, shop]);
  useEffect(() => {
    setResearchFocus(
      section === "competitor-research"
        ? "competitive_landscape"
        : "full_intelligence",
    );
    setDispatchError("");
  }, [section, shop]);
  useEffect(() => {
    setLoading(false);
    setSelected(null);
    setError("");
    setMetrics(null);
    setRows([]);
    if (demo) {
      setRows(
        demoRows(locale)[
          section.endsWith("-research") ? "research" : section
        ] || [],
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
      setError(t("Choose a valid date range", "请选择有效日期范围"));
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
  }, [demo, section, shop, revision, start, end, submittedProject, shops, locale]);
  const filtered = rows.filter(
    (r) =>
      (section === "competitor-research"
        ? r.task_type === "competitive_landscape"
        : section === "product-research"
          ? r.task_type !== "competitive_landscape"
          : true) &&
      JSON.stringify(r).toLowerCase().includes(query.toLowerCase()),
  );
  async function dispatchResearch() {
    const storeUrl = researchUrl.trim();
    if (!shop || !storeUrl || dispatching) return;
    setDispatching(true);
    setDispatchError("");
    try {
      await api("/shop-analysis/run", {
        method: "POST",
        body: JSON.stringify({
          shop_id: shop,
          store_url: storeUrl,
          industry_code:
            shops.find((s) => s.id === shop)?.industry_code || "general",
          research_focus:
            section === "competitor-research"
              ? "competitive_landscape"
              : researchFocus,
          execution_mode: "queued",
        }),
      });
      setRevision((value) => value + 1);
    } catch (e) {
      setDispatchError(
        e instanceof Error ? e.message : t("Dispatch failed", "派发失败"),
      );
    } finally {
      setDispatching(false);
    }
  }
  return (
    <div className="operations-page">
      <div className="resource-header">
        <p className="page-title" tabIndex={-1}>
          {pick(titles[section], locale)}
        </p>
        <IconButton
          icon={RefreshCw}
          label={t("Refresh", "刷新")}
          onClick={() => setRevision((v) => v + 1)}
        />
      </div>
      <div className="operations-toolbar">
        {section !== "memory" && (
          <label>
            {t("Shop", "店铺")}
            <select
              value={shop}
              onChange={(e) => {
                setShop(e.target.value);
                setSubmittedProject("");
              }}
            >
              <option value="" disabled>
                {t("Choose a shop", "选择店铺")}
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
              {t("From", "从")}
              <input
                type="date"
                value={start}
                onChange={(e) => setStart(e.target.value)}
              />
            </label>
            <label>
              {t("To", "至")}
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
              {t("Project", "项目")}
              <input
                required
                value={project}
                onChange={(e) => setProject(e.target.value)}
                placeholder={t("Project name", "项目名称")}
              />
            </label>
            <Button variant="outline" type="submit">
              {t("View", "查看")}
            </Button>
          </form>
        )}
        {section !== "data" && (
          <label className="operations-search">
            <Search size={15} />
            <input
              aria-label={t("Search records", "搜索记录")}
              placeholder={t("Search", "搜索")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </label>
        )}
      </div>
      {(demo || section === "memory") && (
        <p className="muted operations-disclosure">
          {demo
            ? t("Demo data", "演示数据")
            : t("All shops · active memory", "全部店铺 · 有效记忆")}
        </p>
      )}
      {error && (
        <div role="alert" className="alert">
          {error}
          <Button variant="outline" onClick={() => setRevision((v) => v + 1)}>
            {t("Retry", "重试")}
          </Button>
        </div>
      )}
      {loading ? (
        <p role="status" className="empty-state">
          {t("Loading…", "加载中…")}
        </p>
      ) : selected ? (
        <section className="operations-detail">
          <Button variant="ghost" onClick={() => setSelected(null)}>
            <ArrowLeft size={15} />
            {t("Back to list", "返回列表")}
          </Button>
          <p className="operations-detail-title">
            {selected.title ||
              selected.shop_name ||
              label(selected.memory_type, locale)}
          </p>
          <dl>
            {Object.entries(selected)
              .filter(([k]) => k !== "content" && k !== "id")
              .map(([k, v]) => (
                <div key={k}>
                  <dt>
                    {(
                      {
                        title: t("Name", "名称"),
                        shop_name: t("Shop", "店铺"),
                        task_type: t("Research type", "研究类型"),
                        status: t("Status", "状态"),
                        state: t("Status", "状态"),
                        memory_type: t("Memory type", "记忆类型"),
                        memory_scope: t("Scope", "作用域"),
                        source_type: t("Source", "来源"),
                        scheduled_date: t("Date", "日期"),
                        scheduled_time: t("Time", "时间"),
                        channel: t("Channel", "渠道"),
                        created_at: t("Created", "创建时间"),
                        error_message: t("Error", "错误"),
                      } as Record<string, string>
                    )[k] || k}
                  </dt>
                  <dd>
                    {typeof v === "object"
                      ? JSON.stringify(v)
                      : label(String(v ?? "—"), locale)}
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
                ["product_count", t("Products", "产品")],
                ["shopify_revenue", t("Shop revenue", "店铺收入")],
                ["meta_spend", t("Ad spend", "广告花费")],
                ["overall_roas", "ROAS"],
                ["total_impressions", t("Impressions", "曝光")],
                ["total_clicks", t("Clicks", "点击")],
              ].map(([id, name]) => (
                <div key={id}>
                  <dt>{name}</dt>
                  <dd>
                    {metrics[id] === undefined
                      ? "—"
                      : Number(metrics[id]).toLocaleString(
                          locale === "zh" ? "zh-CN" : "en-US",
                        )}
                  </dd>
                </div>
              ))}
            </dl>
          ) : (
            !error && (
              <p className="empty-state">
                {t("Choose a shop and enter a project name", "选择店铺并输入项目名称")}
              </p>
            )
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
                      {r.title || r.shop_name || label(r.memory_type, locale)}
                    </strong>
                    <small>
                      {label(
                        r.channel || r.task_type || r.memory_scope,
                        locale,
                      )}
                    </small>
                  </span>
                  <span>{label(r.state || r.status, locale)}</span>
                  <time>
                    {r.scheduled_date || r.created_at?.slice(0, 10)}{" "}
                    {r.scheduled_time}
                  </time>
                  <ChevronRight size={16} />
                </button>
              ))
            : !error &&
              (query ? (
                <p className="empty-state">
                  {t("No matching records", "没有匹配记录")}
                </p>
              ) : section.endsWith("-research") ? (
                <div className="operations-empty">
                  <p className="operations-empty-title">
                    {t(
                      `No ${pick(titles[section], locale).toLowerCase()} records yet`,
                      `还没有${pick(titles[section], locale)}记录`,
                    )}
                  </p>
                  <p className="operations-empty-copy">
                    {t(
                      `Hand the shop URL to an agent and dispatch ${pick(titles[section], locale).toLowerCase()}. Research records and evidence will appear here when it finishes.`,
                      `把店铺网址交给 agent，派发一次${pick(titles[section], locale)}。完成后，研究记录和证据会出现在这里。`,
                    )}
                  </p>
                  {demo ? (
                    <p className="operations-empty-note">
                      {t(
                        "No research can be dispatched in demo mode.",
                        "演示模式下暂无可派发的研究。",
                      )}
                    </p>
                  ) : (
                    <div className="operations-dispatch">
                      <label>
                        {t("Shop URL", "店铺网址")}
                        <input
                          value={researchUrl}
                          placeholder="https://your-store.com"
                          onChange={(e) => setResearchUrl(e.target.value)}
                        />
                      </label>
                      {section === "product-research" && (
                        <label>
                          {t("Research focus", "研究重点")}
                          <select
                            value={researchFocus}
                            onChange={(e) => setResearchFocus(e.target.value)}
                          >
                            <option value="full_intelligence">
                              {t("Full research", "综合研究")}
                            </option>
                            <option value="store_context">
                              {t("Store research", "店铺研究")}
                            </option>
                            <option value="industry_baseline">
                              {t("Industry research", "行业研究")}
                            </option>
                            <option value="audience_pain_points">
                              {t("Audience research", "受众研究")}
                            </option>
                            <option value="compliance_scan">
                              {t("Compliance research", "合规研究")}
                            </option>
                          </select>
                        </label>
                      )}
                      <Button
                        onClick={() => void dispatchResearch()}
                        disabled={dispatching || !researchUrl.trim()}
                      >
                        {dispatching
                          ? t("Dispatching…", "派发中…")
                          : t("Dispatch research", "派发研究")}
                      </Button>
                    </div>
                  )}
                  {dispatchError && (
                    <p className="operations-empty-error" role="alert">
                      {dispatchError}
                    </p>
                  )}
                </div>
              ) : (
                <p className="empty-state">{t("No records yet", "暂无记录")}</p>
              ))}
        </div>
      )}
    </div>
  );
}
