export default function Loading() {
  return (
    <main className="relative flex min-h-screen flex-col items-center justify-center overflow-hidden px-6 text-center">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="aurora-blob aurora-2" />
      </div>
      <div className="relative z-10 flex flex-col items-center gap-4">
        <div
          className="h-10 w-10 animate-spin rounded-full border-2 border-slate-700 border-t-cyan-400"
          role="status"
          aria-label="加载中 / Loading"
        />
        {/* 实施78 P0-2：加载骨架屏是**服务端组件、拿不到路由语言**，而它出现在每个页面的
            首屏流式 HTML 里——写「无界科技 BOUNDLESS」等于每个英文页都闪一次中文。
            改为语言中性的品牌字标（拉丁字形对中英读者都成立），不做双语分支。 */}
        <div className="text-sm font-medium tracking-wide text-slate-400">
          <span className="text-gradient">BOUNDLESS</span>
        </div>
      </div>
    </main>
  );
}
