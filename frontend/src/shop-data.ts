export type Shop = {
  id: string;
  name: string;
  industry_code: string;
  store_url: string | null;
  description?: string | null;
};
export type ChannelAccount = {
  id: string;
  workspace_id?: string;
  platform: string;
  account_key: string;
  label: string;
  account_id: string | null;
  account_url: string | null;
  credential_env_vars: Record<string, string>;
  sync_settings?: Record<string, unknown>;
  attribution_rules?: Record<string, unknown>;
  status: string;
  is_primary: boolean;
  ready: boolean;
  missing_credentials: string[];
  last_verified_at?: string | null;
};
export type IntegrationConfig = {
  id: string;
  platform: string;
  config_key: string;
  label: string;
  env_var: string;
  is_required: boolean;
  is_set: boolean;
};
export const platforms: Record<
  string,
  {
    name: string;
    tokenKey: string;
    accountKey: string;
    accountLabel: string;
    placeholder: string;
  }
> = {
  shopify: {
    name: "Shopify",
    tokenKey: "access_token",
    accountKey: "store_domain",
    accountLabel: "店铺域名",
    placeholder: "store.myshopify.com",
  },
  meta: {
    name: "Meta Ads",
    tokenKey: "access_token",
    accountKey: "ad_account_id",
    accountLabel: "广告账号 ID",
    placeholder: "act_123456789",
  },
  tiktok: {
    name: "TikTok",
    tokenKey: "access_token",
    accountKey: "advertiser_id",
    accountLabel: "广告主 ID",
    placeholder: "广告主 ID",
  },
  notion: {
    name: "Notion",
    tokenKey: "api_key",
    accountKey: "database_id",
    accountLabel: "数据库 ID",
    placeholder: "数据库 ID",
  },
};
export const envPattern = "CRISPY_API_KEY_[A-Za-z0-9_]+";
export const demoShops: Shop[] = [
  {
    id: "demo-shop-1",
    name: "Atelier Studio",
    industry_code: "apparel",
    store_url: "https://atelier.example",
  },
  {
    id: "demo-shop-2",
    name: "Northline",
    industry_code: "home",
    store_url: "https://northline.example",
  },
];
export const demoConfigs: IntegrationConfig[] = Object.entries(
  platforms,
).flatMap(([platform, definition]) =>
  [definition.tokenKey, definition.accountKey].map((config_key, i) => ({
    id: `${platform}-${config_key}`,
    platform,
    config_key,
    label: i === 0 ? "Token" : definition.accountLabel,
    env_var: `CRISPY_API_KEY_${platform.toUpperCase()}${i === 0 ? "" : "_ACCOUNT"}`,
    is_required: true,
    is_set: platform === "shopify" || platform === "meta",
  })),
);
export const demoAccounts: Record<string, ChannelAccount[]> = {
  "demo-shop-1": Object.entries(platforms).map(([platform, definition], i) => ({
    id: `demo-account-${platform}`,
    workspace_id: "demo-shop-1",
    platform,
    account_key: `${platform}-main`,
    label: `${definition.name} 主账号`,
    account_id: [
      "atelier.myshopify.com",
      "act_123456789",
      "adv_789012",
      "db_123456",
    ][i],
    account_url: null,
    credential_env_vars: {
      [definition.tokenKey]: `CRISPY_API_KEY_${platform.toUpperCase()}`,
    },
    status: platform === "notion" ? "disabled" : "active",
    is_primary: true,
    ready: i < 2,
    missing_credentials: i < 2 ? [] : [definition.tokenKey],
  })),
  "demo-shop-2": [],
};
