import { Suspense } from "react";
import Workbench from "./workbench";

export default function HomePage() {
  return (
    <Suspense fallback={<main className="shell"><div className="panel">加载工作台…</div></main>}>
      <Workbench />
    </Suspense>
  );
}
