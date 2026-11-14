import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  ArrowLeft,
  ArrowUpRight,
  Archive,
  ChevronRight,
  Globe,
  KeyRound,
  LoaderCircle,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Save,
  Store,
} from "lucide-react";
import { Button } from "./components/ui/button";
import { Field, IconButton } from "./components/console-controls";
import { api } from "./data";
import { go } from "./navigation";
import {
  demoAccounts,
  demoConfigs,
  demoShops,
  envPattern,
  platforms,
  type ChannelAccount,
  type IntegrationConfig,
  type Shop,
} from "./shop-data";
import { pick, useI18n, type Locale } from "./i18n";
import { PolishedSurface, AnimatedSelection } from "./components/polish";

const enc = encodeURIComponent;
function accountState(account: ChannelAccount, locale: Locale) {
  const zh = locale === "zh";
  return account.status === "disabled"
    ? { label: zh ? "已停用" : "Disabled", kind: "disabled" }
    : account.status === "archived"
      ? { label: zh ? "已归档" : "Archived", kind: "disabled" }
      : account.status === "warning"
        ? { label: zh ? "需检查" : "Needs attention", kind: "missing" }
        : account.ready
          ? { label: zh ? "配置齐备" : "Configured", kind: "ready" }
          : { label: zh ? "缺少凭证" : "Missing credentials", kind: "missing" };
}
function CredentialState({ account }: { account: ChannelAccount }) {
  const { locale } = useI18n();
  const state = accountState(account, locale);
  return (
    <span className={`credential-state ${state.kind}`}>
      <span />
      {state.label}
    </span>
  );
}

export default function ShopsPage({
  path,
  demo,
  notify,
}: {
  path: string;
  demo: boolean;
  notify: (text: string) => void;
}) {
  const { t, locale } = useI18n();
  const parts = path.split("/").filter(Boolean),
    shared = parts[1] === "shared",
    newShop = parts[1] === "new";
  const [shops, setShops] = useState<Shop[]>(demo ? demoShops : []),
    [configs, setConfigs] = useState<IntegrationConfig[]>(() =>
      demo ? demoConfigs(locale) : [],
    ),
    [accounts, setAccounts] = useState<ChannelAccount[]>([]),
    [demoMap, setDemoMap] = useState(() => demoAccounts(locale));
  const [loading, setLoading] = useState(!demo),
    [accountLoading, setAccountLoading] = useState(false),
    [error, setError] = useState(""),
    [configError, setConfigError] = useState(""),
    [busy, setBusy] = useState(false),
    [revision, setRevision] = useState(0);
  const [showArchived, setShowArchived] = useState(false),
    [archiveTarget, setArchiveTarget] = useState<string | null>(null);
  const mutation = useRef(false),
    mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const currentPath = useRef(path);
  currentPath.current = path;
  const shopId = !shared && !newShop ? parts[1] || shops[0]?.id : undefined,
    shop = shops.find((s) => s.id === shopId),
    shopEdit = parts[2] === "edit",
    accountEdit = parts[2] === "accounts",
    accountId = parts[3],
    account = accounts.find((a) => a.id === accountId),
    globalConfig = shared
      ? configs.find(
          (c) => c.platform === parts[2] && c.config_key === parts[3],
        )
      : undefined;
  useEffect(() => {
    document.title = `Crispy · ${
      accountEdit
        ? t("Account credentials", "账号凭证")
        : newShop
          ? t("Add shop", "添加店铺")
          : t("Shops & credentials", "店铺与凭证")
    }`;
    setError("");
    setArchiveTarget(null);
  }, [path, accountEdit, newShop, t]);
  useEffect(() => {
    if (demo) setDemoMap(demoAccounts(locale));
  }, [demo, locale]);
  useEffect(() => {
    const frame = requestAnimationFrame(() =>
      document.querySelector<HTMLElement>(".page-title")?.focus(),
    );
    return () => cancelAnimationFrame(frame);
  }, [path]);
  useEffect(() => {
    if (demo) return;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    Promise.allSettled([
      api<{ shops: Shop[] }>("/shops", { signal: controller.signal }),
      api<IntegrationConfig[]>("/integration-configs", {
        signal: controller.signal,
      }),
    ]).then(([shopResult, configResult]) => {
      if (controller.signal.aborted) return;
      if (shopResult.status === "fulfilled") setShops(shopResult.value.shops);
      else setError(shopResult.reason.message);
      if (configResult.status === "fulfilled") {
        setConfigs(configResult.value);
        setConfigError("");
      } else setConfigError(configResult.reason.message);
      setLoading(false);
    });
    return () => controller.abort();
  }, [demo, revision]);
  useEffect(() => {
    setAccounts([]);
    if (!shopId) return;
    if (demo) {
      setAccounts(
        (demoMap[shopId] || []).filter(
          (a) => showArchived || a.status !== "archived",
        ),
      );
      return;
    }
    const controller = new AbortController();
    setAccountLoading(true);
    setError("");
    api<{ accounts: ChannelAccount[] }>(
      `/shops/${enc(shopId)}/channel-accounts${showArchived ? "?include_archived=true" : ""}`,
      { signal: controller.signal },
    )
      .then((data) => setAccounts(data.accounts))
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setAccountLoading(false);
      });
    return () => controller.abort();
  }, [shopId, demo, demoMap, revision, showArchived]);
  function notifySaved() {
    notify(demo ? t("Demo configuration saved", "演示配置已保存") : t("Saved", "已保存"));
  }
  async function saveShop(
    value: { name: string; industry_code: string; store_url: string },
    existing?: Shop,
  ) {
    const submitted = path;
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    try {
      const result = demo
        ? { ...value, id: existing?.id || `demo-shop-${Date.now()}` }
        : await api<Shop>(existing ? `/shops/${enc(existing.id)}` : "/shops", {
            method: existing ? "PATCH" : "POST",
            body: JSON.stringify(value),
          });
      setShops((rows) =>
        existing
          ? rows.map((s) => (s.id === result.id ? result : s))
          : [...rows, result],
      );
      if (mounted.current && currentPath.current === submitted) {
        notifySaved();
        go(`/shops/${result.id}`);
      }
    } catch (e) {
      if (mounted.current && currentPath.current === submitted)
        setError((e as Error).message);
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }
  async function saveAccount(
    value: Omit<ChannelAccount, "id" | "ready" | "missing_credentials">,
    existing?: ChannelAccount,
  ) {
    if (!shopId) return;
    const submitted = path;
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    try {
      let result: ChannelAccount;
      if (demo) {
        const definition = platforms[value.platform];
        const missing = definition
          ? [definition.tokenKey, definition.accountKey].filter((key) => {
              const env = value.credential_env_vars[key];
              const envReady =
                !!env &&
                demoConfigs(locale).some(
                  (c) => c.env_var === env && c.is_set,
                );
              const accountFallback =
                key === definition.accountKey &&
                !!(
                  value.account_id ||
                  (value.platform !== "notion" ? value.account_key : "")
                );
              return !envReady && !accountFallback;
            })
          : [];
        result = {
          ...value,
          id: existing?.id || `demo-account-${Date.now()}`,
          ready: missing.length === 0,
          missing_credentials: [...new Set(missing)],
        };
        setDemoMap((map) => ({
          ...map,
          [shopId]: (existing
            ? (map[shopId] || []).map((a) =>
                a.id === existing.id ? result : a,
              )
            : [...(map[shopId] || []), result]
          ).map((a) =>
            result.is_primary &&
            a.platform === result.platform &&
            a.id !== result.id
              ? { ...a, is_primary: false }
              : a,
          ),
        }));
      } else {
        result = await api<ChannelAccount>(
          `/shops/${enc(shopId)}/channel-accounts${existing ? "/" + enc(existing.id) : ""}`,
          { method: existing ? "PATCH" : "POST", body: JSON.stringify(value) },
        );
        setRevision((r) => r + 1);
      }
      if (mounted.current && currentPath.current === submitted) {
        notifySaved();
        go(`/shops/${shopId}`);
      }
    } catch (e) {
      if (mounted.current && currentPath.current === submitted)
        setError((e as Error).message);
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }
  async function toggleAccount(value: ChannelAccount) {
    if (!shopId) return;
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    const status = ["disabled", "archived"].includes(value.status)
      ? "active"
      : "disabled";
    try {
      if (demo)
        setDemoMap((map) => ({
          ...map,
          [shopId]: (map[shopId] || []).map((a) =>
            a.id === value.id ? { ...a, status } : a,
          ),
        }));
      else {
        await api(`/shops/${enc(shopId)}/channel-accounts/${enc(value.id)}`, {
          method: "PATCH",
          body: JSON.stringify({ status }),
        });
        setRevision((r) => r + 1);
      }
      notify(
        demo
          ? t("Demo: account status updated", "演示：已更新账号状态")
          : t("Account status updated", "已更新账号状态"),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }
  async function saveGlobal(value: IntegrationConfig, env_var: string) {
    const submitted = path;
    if (mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    try {
      const result = demo
        ? {
            ...value,
            env_var,
            is_set: demoConfigs(locale).some(
              (c) => c.env_var === env_var && c.is_set,
            ),
          }
        : await api<IntegrationConfig>(
            `/integration-configs/${enc(value.platform)}/${enc(value.config_key)}`,
            { method: "PATCH", body: JSON.stringify({ env_var }) },
          );
      setConfigs((list) => list.map((c) => (c.id === value.id ? result : c)));
      if (mounted.current && currentPath.current === submitted) {
        notifySaved();
        go("/shops/shared");
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }
  async function archiveAccount(value: ChannelAccount) {
    if (!shopId || mutation.current) return;
    mutation.current = true;
    setBusy(true);
    setError("");
    try {
      if (demo)
        setDemoMap((map) => ({
          ...map,
          [shopId]: (map[shopId] || []).map((a) =>
            a.id === value.id
              ? { ...a, status: "archived", is_primary: false }
              : a,
          ),
        }));
      else {
        await api<void>(
          `/shops/${enc(shopId)}/channel-accounts/${enc(value.id)}`,
          { method: "DELETE" },
        );
        setRevision((r) => r + 1);
      }
      setArchiveTarget(null);
      if (mounted.current)
        notify(
          demo
            ? t("Demo: account archived", "演示：账号已归档")
            : t("Account archived", "账号已归档"),
        );
    } catch (e) {
      if (mounted.current) setError((e as Error).message);
    } finally {
      mutation.current = false;
      setBusy(false);
    }
  }
  const editing = newShop || shopEdit || accountEdit || !!globalConfig;
  return (
    <div className="shops-page">
      <div className="breadcrumb">
        <a href="#/shops">{t("Settings", "设置")}</a>
        <ChevronRight size={12} />
        <a href="#/shops">{t("Shops & credentials", "店铺与凭证")}</a>
        {editing && (
          <>
            <ChevronRight size={12} />
            <span>
              {accountEdit
                ? t("Account setup", "账号配置")
                : globalConfig
                  ? t("Shared mapping", "共享映射")
                  : newShop
                    ? t("Add shop", "添加店铺")
                    : t("Edit shop", "编辑店铺")}
            </span>
          </>
        )}
      </div>
      <div className="resource-header">
        <div className="resource-name">
          {editing && (
            <IconButton
              icon={ArrowLeft}
              label={t("Back to shop credentials", "返回店铺凭证")}
              onClick={() =>
                go(
                  shared
                    ? "/shops/shared"
                    : shopId
                      ? `/shops/${shopId}`
                      : "/shops",
                )
              }
            />
          )}
          <p className="page-title" tabIndex={-1}>
            {newShop
              ? t("Add shop", "添加店铺")
              : shopEdit
                ? t("Edit shop", "编辑店铺")
                : accountEdit
                  ? accountId === "new"
                    ? t("Add channel account", "添加渠道账号")
                    : t("Edit account credentials", "编辑账号凭证")
                  : globalConfig
                    ? t("Edit shared mapping", "编辑共享映射")
                    : t("Shops & credentials", "店铺与凭证")}
          </p>
        </div>
        {!editing && (
          <Button onClick={() => go("/shops/new")}>
            <Plus size={15} />
            {t("Add shop", "添加店铺")}
          </Button>
        )}
      </div>
      {error && (
        <div className="alert" role="alert">
          <span>{error}</span>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() =>
              editing &&
              (newShop || !!account || (shopEdit && !!shop) || !!globalConfig)
                ? setError("")
                : setRevision((r) => r + 1)
            }
          >
            <RefreshCw size={14} />
            {editing &&
            (newShop || !!account || (shopEdit && !!shop) || !!globalConfig)
              ? t("Keep editing", "继续编辑")
              : t("Retry", "重试")}
          </Button>
        </div>
      )}
      {newShop || shopEdit ? (
        shopEdit && !shop ? (
          <Empty loading={loading} text={t("Shop not found", "找不到店铺")} />
        ) : (
          <ShopForm
            key={shop?.id || "new"}
            shop={shopEdit ? shop : undefined}
            busy={busy}
            save={saveShop}
          />
        )
      ) : accountEdit ? (
        accountLoading ? (
          <Empty loading text={t("Loading accounts", "正在加载账号")} />
        ) : !shop ? (
          <Empty text={t("Shop not found", "找不到店铺")} />
        ) : accountId !== "new" && !account ? (
          <Empty text={t("Account not found", "找不到账号")} />
        ) : (
          <AccountForm
            key={`${shop.id}-${accountId}`}
            shop={shop}
            account={account}
            busy={busy}
            configs={configs}
            save={saveAccount}
          />
        )
      ) : globalConfig ? (
        <GlobalForm
          key={globalConfig.id}
          config={globalConfig}
          busy={busy}
          save={saveGlobal}
        />
      ) : (
        <>
          <div
            className="resource-tabs"
            role="group"
            aria-label={t("Credential scope", "凭证范围")}
          >
            <AnimatedSelection active={shared ? "shared" : "shops"}>
              <button
                data-selection-id="shops"
                aria-pressed={!shared}
                className={!shared ? "active" : ""}
                onClick={() => go(shopId ? `/shops/${shopId}` : "/shops")}
              >
                {t("Shop accounts", "店铺账号")}
              </button>
              <button
                data-selection-id="shared"
                aria-pressed={shared}
                className={shared ? "active" : ""}
                onClick={() => go("/shops/shared")}
              >
                {t("Shared mappings", "共享映射")}
              </button>
            </AnimatedSelection>
          </div>
          {shared ? (
            <>
              <p className="credential-note">
                <KeyRound size={14} />
                {t(
                  "Shared environment-variable mappings apply to every shop.",
                  "共享环境变量映射，适用于所有店铺。",
                )}
              </p>
              {configError ? (
                <div className="alert" role="alert">
                  {configError}
                  <Button
                    variant="outline"
                    onClick={() => setRevision((r) => r + 1)}
                  >
                    {t("Retry", "重试")}
                  </Button>
                </div>
              ) : (
                <section className="surface">
                  <div className="table-scroll">
                    <table className="config-table">
                      <thead>
                        <tr>
                          <th>{t("Platform / field", "平台 / 字段")}</th>
                          <th>{t("Environment variable", "环境变量")}</th>
                          <th>{t("Local status", "本地状态")}</th>
                          <th>
                            <span className="sr-only">{t("Edit", "编辑")}</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {configs.map((c) => (
                          <tr key={c.id}>
                            <td>
                              <strong>
                                {platforms[c.platform]?.name || c.platform}
                              </strong>
                              <small>
                                {platforms[c.platform]?.accountKey ===
                                c.config_key
                                  ? pick(
                                      platforms[c.platform].accountLabel,
                                      locale,
                                    )
                                  : "Token"}
                              </small>
                            </td>
                            <td>
                              <code>{c.env_var || t("Unbound", "未绑定")}</code>
                            </td>
                            <td>
                              <span
                                className={`credential-state ${c.is_set ? "ready" : "missing"}`}
                              >
                                <span />
                                {c.is_set
                                  ? t("Environment variable set", "环境变量已设置")
                                  : t("Not set", "未设置")}
                              </span>
                            </td>
                            <td>
                              <IconButton
                                icon={Pencil}
                                label={t(
                                  `Edit ${c.platform} ${c.config_key}`,
                                  `编辑 ${c.platform} ${c.config_key}`,
                                )}
                                onClick={() =>
                                  go(
                                    `/shops/shared/${c.platform}/${c.config_key}`,
                                  )
                                }
                              />
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  {!configs.length && (
                    <Empty
                      loading={loading}
                      text={t("No shared mappings yet", "暂无共享映射")}
                    />
                  )}
                </section>
              )}
            </>
          ) : (
            <>
              {loading ? (
                <Empty loading text={t("Loading shops", "正在加载店铺")} />
              ) : !shops.length ? (
                <div className="empty-state">
                  <Store size={28} />
                  <p>{t("No shops yet", "尚未添加店铺")}</p>
                  <Button onClick={() => go("/shops/new")}>
                    <Plus size={15} />
                    {t("Add shop", "添加店铺")}
                  </Button>
                </div>
              ) : (
                <>
                  <div className="shop-picker">
                    <label htmlFor="shop-selector">
                      {t("Current shop", "当前店铺")}
                    </label>
                    <select
                      id="shop-selector"
                      value={shopId || ""}
                      onChange={(e) => go(`/shops/${e.target.value}`)}
                    >
                      {shops.map((s) => (
                        <option key={s.id} value={s.id}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                    <IconButton
                      icon={RefreshCw}
                      label={t("Check local configuration", "检查本地配置")}
                      disabled={accountLoading || busy}
                      onClick={() =>
                        demo
                          ? notify(
                              t(
                                "Demo mode: no platform connected",
                                "演示模式：未连接平台",
                              ),
                            )
                          : setRevision((r) => r + 1)
                      }
                    />
                  </div>
                  {shop && (
                    <>
                      <PolishedSurface className="shop-identity">
                        <div className="shop-symbol">
                          <Store size={23} />
                        </div>
                        <div>
                          <p>{shop.name}</p>
                          <div className="shop-metadata">
                            {shop.store_url ? (
                              <a
                                href={safeUrl(shop.store_url)}
                                target="_blank"
                                rel="noreferrer"
                              >
                                <Globe size={12} />
                                {shop.store_url}
                                <ArrowUpRight size={12} />
                              </a>
                            ) : (
                              <span>{t("No shop URL", "未填写店铺网址")}</span>
                            )}
                            <span>{shop.industry_code}</span>
                          </div>
                        </div>
                        <IconButton
                          icon={Pencil}
                          label={t("Edit shop", "编辑店铺")}
                          onClick={() => go(`/shops/${shop.id}/edit`)}
                        />
                      </PolishedSurface>
                      <section className="surface channel-surface">
                        <div className="section-toolbar">
                          <p>
                            {t("Channel accounts", "渠道账号")}{" "}
                            <span className="muted">{accounts.length}</span>
                          </p>
                          <label className="archive-filter">
                            <input
                              type="checkbox"
                              checked={showArchived}
                              onChange={(e) =>
                                setShowArchived(e.target.checked)
                              }
                            />
                            {t("Archived", "已归档")}
                          </label>
                          <Button
                            variant="outline"
                            disabled={accountLoading}
                            onClick={() => go(`/shops/${shop.id}/accounts/new`)}
                          >
                            <Plus size={14} />
                            {t("Add account", "添加账号")}
                          </Button>
                        </div>
                        <div className="table-scroll">
                          <table className="accounts-table">
                            <thead>
                              <tr>
                                <th>{t("Platform / account", "平台 / 账号")}</th>
                                <th>{t("Local configuration", "本地配置")}</th>
                                <th>{t("Credential binding", "凭证绑定")}</th>
                                <th>
                                  <span className="sr-only">{t("Actions", "操作")}</span>
                                </th>
                              </tr>
                            </thead>
                            <tbody>
                              {accounts.map((a) => (
                                <tr key={a.id}>
                                  <td>
                                    <a
                                      href={`#/shops/${shop.id}/accounts/${a.id}`}
                                      className="channel-name"
                                    >
                                      <span
                                        className={`platform-symbol ${a.platform}`}
                                      >
                                        <Store size={17} />
                                      </span>
                                      <span>
                                        <strong>
                                          {a.label ||
                                            platforms[a.platform]?.name ||
                                            a.platform}
                                        </strong>
                                        <small>
                                          {platforms[a.platform]?.name ||
                                            a.platform}{" "}
                                          · {a.account_id || a.account_key}
                                        </small>
                                      </span>
                                    </a>
                                  </td>
                                  <td>
                                    <CredentialState account={a} />
                                    {a.status !== "disabled" &&
                                      !!a.missing_credentials.length && (
                                        <small className="missing-field">
                                          {t("Missing", "缺少")}{" "}
                                          {a.missing_credentials.join(
                                            locale === "zh" ? "、" : ", ",
                                          )}
                                        </small>
                                      )}
                                  </td>
                                  <td>
                                    <code>
                                      {a.credential_env_vars[
                                        platforms[a.platform]?.tokenKey ||
                                          "access_token"
                                      ] || t("Unbound", "未绑定")}
                                    </code>
                                  </td>
                                  <td>
                                    <div className="row-actions">
                                      <IconButton
                                        icon={Pencil}
                                        label={t(
                                          `Edit ${a.label || a.account_key}`,
                                          `编辑 ${a.label || a.account_key}`,
                                        )}
                                        disabled={busy}
                                        onClick={() =>
                                          go(
                                            `/shops/${shop.id}/accounts/${a.id}`,
                                          )
                                        }
                                      />
                                      <IconButton
                                        icon={Power}
                                        label={
                                          a.status === "archived"
                                            ? t(
                                                `Restore ${a.label || a.account_key}`,
                                                `恢复 ${a.label || a.account_key}`,
                                              )
                                            : a.status === "disabled"
                                              ? t(
                                                  `Enable ${a.label || a.account_key}`,
                                                  `启用 ${a.label || a.account_key}`,
                                                )
                                              : t(
                                                  `Disable ${a.label || a.account_key}`,
                                                  `停用 ${a.label || a.account_key}`,
                                                )
                                        }
                                        disabled={busy}
                                        onClick={() => void toggleAccount(a)}
                                      />
                                      {a.status !== "archived" && (
                                        <IconButton
                                          icon={Archive}
                                          label={t(
                                            `Archive ${a.label || a.account_key}`,
                                            `归档 ${a.label || a.account_key}`,
                                          )}
                                          disabled={busy}
                                          onClick={() => setArchiveTarget(a.id)}
                                        />
                                      )}
                                    </div>
                                    {archiveTarget === a.id && (
                                      <div className="archive-confirm">
                                        <span>{t("Archive this account?", "归档此账号？")}</span>
                                        <Button
                                          variant="outline"
                                          disabled={busy}
                                          onClick={() => setArchiveTarget(null)}
                                        >
                                          {t("Cancel", "取消")}
                                        </Button>
                                        <Button
                                          disabled={busy}
                                          onClick={() => void archiveAccount(a)}
                                        >
                                          {t("Archive", "归档")}
                                        </Button>
                                      </div>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        {!accounts.length && (
                          <Empty
                            loading={accountLoading}
                            text={t("No channel accounts yet", "尚未添加渠道账号")}
                          />
                        )}
                      </section>
                    </>
                  )}
                </>
              )}
            </>
          )}
        </>
      )}
    </div>
  );
}
function Empty({ loading = false, text }: { loading?: boolean; text: string }) {
  return (
    <div className="empty-state">
      {loading ? (
        <LoaderCircle className="spin" size={24} />
      ) : (
        <KeyRound size={24} />
      )}
      <p>{text}</p>
    </div>
  );
}
function safeUrl(value: string) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol) ? url.href : undefined;
  } catch {
    return undefined;
  }
}
function ShopForm({
  shop,
  busy,
  save,
}: {
  shop?: Shop;
  busy: boolean;
  save: (
    value: { name: string; industry_code: string; store_url: string },
    existing?: Shop,
  ) => Promise<void>;
}) {
  const { t } = useI18n();
  const [name, setName] = useState(shop?.name || ""),
    [industry, setIndustry] = useState(shop?.industry_code || ""),
    [url, setUrl] = useState(shop?.store_url || "");
  return (
    <form
      className="surface credential-form"
      onSubmit={(e) => {
        e.preventDefault();
        if (name.trim())
          void save(
            {
              name: name.trim(),
              industry_code: industry.trim() || "general",
              store_url: url.trim(),
            },
            shop,
          );
      }}
    >
      <div className="form-body">
        <Field label={t("Shop name", "店铺名称")}>
          <input
            required
            maxLength={128}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </Field>
        <div className="form-row">
          <Field label={t("Industry", "行业")}>
            <input
              value={industry}
              maxLength={128}
              placeholder={t("e.g. apparel", "例如 apparel")}
              onChange={(e) => setIndustry(e.target.value)}
            />
          </Field>
          <Field label={t("Shop URL", "店铺网址")}>
            <input
              type="url"
              value={url}
              maxLength={512}
              placeholder="https://store.example"
              onChange={(e) => setUrl(e.target.value)}
            />
          </Field>
        </div>
      </div>
      <FormActions
        busy={busy}
        cancel={() => go(shop ? `/shops/${shop.id}` : "/shops")}
      />
    </form>
  );
}
function AccountForm({
  shop,
  account,
  busy,
  configs,
  save,
}: {
  shop: Shop;
  account?: ChannelAccount;
  busy: boolean;
  configs: IntegrationConfig[];
  save: (
    value: Omit<ChannelAccount, "id" | "ready" | "missing_credentials">,
    existing?: ChannelAccount,
  ) => Promise<void>;
}) {
  const { t, locale } = useI18n();
  const [platform, setPlatform] = useState(account?.platform || "shopify"),
    [label, setLabel] = useState(account?.label || ""),
    [key, setKey] = useState(account?.account_key || ""),
    [id, setId] = useState(account?.account_id || ""),
    [token, setToken] = useState(
      account?.credential_env_vars[
        platforms[platform]?.tokenKey || "access_token"
      ] || "",
    ),
    [accountEnv, setAccountEnv] = useState(
      account?.credential_env_vars[
        platforms[platform]?.accountKey || "account_id"
      ] || "",
    ),
    [status, setStatus] = useState(account?.status || "active"),
    [primary, setPrimary] = useState(account?.is_primary || false);
  const definition = platforms[platform] || {
    name: platform,
    tokenKey: "access_token",
    accountKey: "account_id",
    accountLabel: { en: "Account ID", zh: "账号 ID" },
    placeholder: { en: "Account ID", zh: "账号 ID" },
  };
  function submit(e: FormEvent) {
    e.preventDefault();
    if (!key.trim() || !label.trim() || (!id.trim() && !accountEnv.trim()))
      return;
    void save(
      {
        platform,
        account_key: key.trim(),
        label: label.trim(),
        account_id: id.trim(),
        account_url: account?.account_url || null,
        credential_env_vars: {
          ...account?.credential_env_vars,
          [definition.tokenKey]: token.trim(),
          [definition.accountKey]: accountEnv.trim(),
        },
        status,
        is_primary: primary,
      },
      account,
    );
  }
  return (
    <form className="surface credential-form" onSubmit={submit}>
      <div className="form-heading">
        <p>{shop.name}</p>
        <span className="muted">{t("Shop-specific binding", "店铺专属绑定")}</span>
      </div>
      <div className="form-body">
        <div className="form-row">
          <Field label={t("Platform", "平台")}>
            <select
              disabled={!!account}
              value={platform}
              onChange={(e) => {
                setPlatform(e.target.value);
                setId("");
                setToken("");
                setAccountEnv("");
              }}
            >
              {!platforms[platform] && (
                <option value={platform}>{platform}</option>
              )}
              {Object.entries(platforms).map(([value, p]) => (
                <option key={value} value={value}>
                  {p.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label={t("Account name", "账号名称")}>
            <input
              required
              maxLength={128}
              value={label}
              placeholder={t("e.g. Shopify primary", "例如 Shopify 主账号")}
              onChange={(e) => setLabel(e.target.value)}
            />
          </Field>
        </div>
        <div className="form-row">
          <Field label={t("Account key", "账号标识")}>
            <input
              required
              maxLength={128}
              value={key}
              placeholder={t("e.g. shopify-main", "例如 shopify-main")}
              onChange={(e) => setKey(e.target.value)}
            />
          </Field>
          <Field label={pick(definition.accountLabel, locale)}>
            <input
              required={!accountEnv.trim()}
              maxLength={128}
              value={id}
              placeholder={pick(definition.placeholder, locale)}
              onChange={(e) => setId(e.target.value)}
            />
          </Field>
        </div>
        <div className="credential-divider" />
        <Field
          label={
            platform === "notion"
              ? t("API key environment variable", "API Key 环境变量")
              : t("Access token environment variable", "Access Token 环境变量")
          }
        >
          <input
            pattern={envPattern}
            maxLength={128}
            value={token}
            list="credential-env-suggestions"
            placeholder={`CRISPY_API_KEY_${platform.toUpperCase()}`}
            onChange={(e) => setToken(e.target.value)}
          />
        </Field>
        <Field
          label={t(
            `${pick(definition.accountLabel, locale)} environment variable (optional)`,
            `${pick(definition.accountLabel, locale)}环境变量（可选）`,
          )}
        >
          <input
            pattern={envPattern}
            maxLength={128}
            value={accountEnv}
            list="credential-env-suggestions"
            placeholder={t("Leave blank to use the account info above", "留空时使用上方账号信息")}
            onChange={(e) => setAccountEnv(e.target.value)}
          />
        </Field>
        <datalist id="credential-env-suggestions">
          {configs
            .filter((c) => c.platform === platform)
            .map((c) => (
              <option key={c.id} value={c.env_var} />
            ))}
        </datalist>
        <p className="inline-note">
          {t(
            "Enter the CRISPY_API_KEY_* variable name, not the secret value.",
            "填写 CRISPY_API_KEY_* 变量名，不填写密钥值。",
          )}
        </p>
        <div className="credential-divider" />
        <div className="form-row">
          <Field label={t("Account status", "账号状态")}>
            <select value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="active">{t("Enabled", "启用")}</option>
              <option value="disabled">{t("Disabled", "停用")}</option>
              {!["active", "disabled"].includes(status) && (
                <option value={status}>{status}</option>
              )}
            </select>
          </Field>
          <label className="checkbox-field">
            <input
              type="checkbox"
              checked={primary}
              onChange={(e) => setPrimary(e.target.checked)}
            />
            {t("Set as the primary account for this platform", "设为该平台主账号")}
          </label>
        </div>
      </div>
      <FormActions busy={busy} cancel={() => go(`/shops/${shop.id}`)} />
    </form>
  );
}
function GlobalForm({
  config,
  busy,
  save,
}: {
  config: IntegrationConfig;
  busy: boolean;
  save: (config: IntegrationConfig, env: string) => Promise<void>;
}) {
  const { t } = useI18n();
  const [env, setEnv] = useState(config.env_var);
  return (
    <form
      className="surface credential-form"
      onSubmit={(e) => {
        e.preventDefault();
        void save(config, env.trim());
      }}
    >
      <div className="form-heading">
        <p>
          {platforms[config.platform]?.name || config.platform} /{" "}
          {config.config_key}
        </p>
      </div>
      <div className="form-body">
        <Field label={t("Environment variable name", "环境变量名")}>
          <input
            pattern={envPattern}
            value={env}
            maxLength={128}
            onChange={(e) => setEnv(e.target.value)}
          />
        </Field>
        <p className="inline-note">
          {t(
            "Shared mappings apply to every shop. Shop-specific bindings are configured on the account page.",
            "共享映射适用于所有店铺。店铺专属绑定在账号页面配置。",
          )}
        </p>
      </div>
      <FormActions busy={busy} cancel={() => go("/shops/shared")} />
    </form>
  );
}
function FormActions({ busy, cancel }: { busy: boolean; cancel: () => void }) {
  const { t } = useI18n();
  return (
    <div className="form-actions">
      <Button variant="outline" disabled={busy} onClick={cancel}>
        {t("Cancel", "取消")}
      </Button>
      <Button type="submit" disabled={busy}>
        {busy ? (
          <LoaderCircle size={15} className="spin" />
        ) : (
          <Save size={15} />
        )}
        {t("Save", "保存")}
      </Button>
    </div>
  );
}
