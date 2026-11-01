export function go(path: string) {
  location.hash = path;
  window.scrollTo(0, 0);
}
