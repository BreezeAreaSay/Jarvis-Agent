import { PageHeader } from "@/components/PageHeader";

export default function Page({ route }: { route: string[] }) {
  return (
    <div className="page">
      <PageHeader title="Расширения" />
      <div className="page-body">
        <div className="card faint">Раздел в разработке ({route.join("/") || "главная"}).</div>
      </div>
    </div>
  );
}
