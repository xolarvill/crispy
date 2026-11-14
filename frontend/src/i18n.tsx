import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

export type Locale = "en" | "zh";

/** A string that has an English and a Chinese form. */
export type Localized = { en: string; zh: string };

const STORAGE_KEY = "crispy.locale";

function readLocale(): Locale {
  try {
    return localStorage.getItem(STORAGE_KEY) === "zh" ? "zh" : "en";
  } catch {
    return "en";
  }
}

/** Locale outside React, for modules that cannot use hooks (e.g. the API client). */
export function currentLocale(): Locale {
  return readLocale();
}

/** Pick the right form of a localised value. */
export function pick(value: Localized, locale: Locale): string {
  return locale === "zh" ? value.zh : value.en;
}

type I18nValue = {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  /** t("English", "中文") — returns the active language. */
  t: (en: string, zh: string) => string;
};

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(readLocale);
  useEffect(() => {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  }, [locale]);
  const setLocale = useCallback((next: Locale) => {
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      /* storage can be unavailable; the in-memory choice still applies */
    }
    setLocaleState(next);
  }, []);
  const t = useCallback(
    (en: string, zh: string) => (locale === "zh" ? zh : en),
    [locale],
  );
  const value = useMemo(
    () => ({ locale, setLocale, t }),
    [locale, setLocale, t],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}

export function formatDateTime(locale: Locale, value: string): string {
  return new Intl.DateTimeFormat(locale === "zh" ? "zh-CN" : "en-US", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}

/** Compact EN / 中文 switch used in the workspace rail. */
export function LocaleSwitch({ className = "" }: { className?: string }) {
  const { locale, setLocale, t } = useI18n();
  return (
    <div
      className={`locale-switch ${className}`.trim()}
      role="group"
      aria-label={t("Language", "语言")}
    >
      <button
        type="button"
        aria-pressed={locale === "en"}
        className={locale === "en" ? "active" : ""}
        onClick={() => setLocale("en")}
      >
        EN
      </button>
      <button
        type="button"
        aria-pressed={locale === "zh"}
        className={locale === "zh" ? "active" : ""}
        onClick={() => setLocale("zh")}
      >
        中文
      </button>
    </div>
  );
}
