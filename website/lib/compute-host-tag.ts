/** 媒体灯标签：快照带 endpoint 就从 URL 取主机尾号，老快照回落写死值。 */
export function hostTag(endpoint?: string, legacy = ""): string {
  const m = /192\.168\.\d+\.(\d+)(?::(\d+))?/.exec(endpoint || "");
  return m ? `@${m[1]}${m[2] ? ":" + m[2] : ""}` : legacy;
}
