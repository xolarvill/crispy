/* Bilingual support for the legacy server-rendered dashboard.
 *
 * The server translates the initial HTML. This script applies the same
 * dictionary to labels that JavaScript inserts after load, and provides the
 * language switch used by the global rail.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "crispy.locale";
  var ATTRIBUTES = ["title", "placeholder", "aria-label", "alt"];
  var SKIP_TAGS = { script: 1, style: 1, pre: 1, code: 1, textarea: 1, template: 1 };

  function readLocale() {
    try {
      var query = new URLSearchParams(window.location.search).get("lang");
      if (query) return query.toLowerCase().indexOf("zh") === 0 ? "zh" : "en";
    } catch (error) {
      /* older browsers */
    }
    var cookie = document.cookie.match(
      new RegExp("(?:^|;\\s*)" + STORAGE_KEY.replace(".", "\\.") + "=([^;]*)")
    );
    if (cookie && cookie[1] === "zh") return "zh";
    try {
      return localStorage.getItem(STORAGE_KEY) === "zh" ? "zh" : "en";
    } catch (error) {
      return "en";
    }
  }

  window.setCrispyLocale = function (next) {
    var locale = next === "zh" ? "zh" : "en";
    try {
      localStorage.setItem(STORAGE_KEY, locale);
    } catch (error) {
      /* storage can be unavailable in private windows */
    }
    document.cookie =
      STORAGE_KEY + "=" + locale + "; path=/; max-age=31536000; SameSite=Lax";
    window.location.reload();
  };

  var locale = readLocale();
  document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  if (locale !== "zh") return;

  var data = window.CRISPY_I18N || { dict: {}, patterns: [] };
  var dictionary = data.dict || {};
  var patterns = (data.patterns || []).map(function (entry) {
    var replacement = String(entry[1]).replace(/\\(\d+)/g, function (_, index) {
      return "$" + index;
    });
    return [new RegExp("^(?:" + entry[0] + ")$"), replacement];
  });

  function translateText(value) {
    if (typeof value !== "string" || !value) return value;
    var match = /^(\s*)([\s\S]*?)(\s*)$/.exec(value);
    if (!match) return value;
    var lead = match[1];
    var body = match[2];
    var trail = match[3];
    if (!body) return value;
    if (Object.prototype.hasOwnProperty.call(dictionary, body)) {
      return lead + dictionary[body] + trail;
    }
    for (var i = 0; i < patterns.length; i += 1) {
      if (patterns[i][0].test(body)) {
        return lead + body.replace(patterns[i][0], patterns[i][1]) + trail;
      }
    }
    return value;
  }

  function translateAttributes(element) {
    if (!element || element.nodeType !== 1 || !element.getAttribute) return;
    for (var i = 0; i < ATTRIBUTES.length; i += 1) {
      var name = ATTRIBUTES[i];
      var value = element.getAttribute(name);
      if (!value) continue;
      var translated = translateText(value);
      if (translated !== value) element.setAttribute(name, translated);
    }
  }

  function translateNode(node) {
    if (!node) return;
    if (node.nodeType === 3) {
      var translated = translateText(node.nodeValue);
      if (translated !== node.nodeValue) node.nodeValue = translated;
      return;
    }
    if (node.nodeType !== 1) return;
    var tag = node.tagName ? node.tagName.toLowerCase() : "";
    if (SKIP_TAGS[tag]) return;
    translateAttributes(node);
    for (var child = node.firstChild; child; child = child.nextSibling) {
      translateNode(child);
    }
  }

  function translateDocument() {
    if (locale !== "zh") return;
    translateNode(document.body);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", translateDocument, { once: true });
  } else {
    translateDocument();
  }

  var queue = [];
  var scheduled = false;

  function flush() {
    scheduled = false;
    var nodes = queue.splice(0, queue.length);
    for (var i = 0; i < nodes.length; i += 1) translateNode(nodes[i]);
  }

  function enqueue(node) {
    queue.push(node);
    if (scheduled) return;
    scheduled = true;
    if (typeof queueMicrotask === "function") queueMicrotask(flush);
    else setTimeout(flush, 0);
  }

  if (typeof MutationObserver !== "function") return;

  new MutationObserver(function (mutations) {
    if (locale !== "zh") return;
    for (var i = 0; i < mutations.length; i += 1) {
      var mutation = mutations[i];
      if (mutation.type === "characterData") {
        enqueue(mutation.target);
        continue;
      }
      for (var j = 0; j < mutation.addedNodes.length; j += 1) {
        enqueue(mutation.addedNodes[j]);
      }
    }
  }).observe(document.documentElement, {
    childList: true,
    characterData: true,
    subtree: true,
  });
})();
