import { useApp } from "@/store/app";
import { Button } from "@/ui";

export default function Onboarding() {
  const patch = useApp((s) => s.patchSettings);
  return (
    <div className="page center">
      <div className="card" style={{ maxWidth: 420 }}>
        <h2 className="h-section">Добро пожаловать в Jarvis</h2>
        <p className="muted">Мастер первого запуска в разработке.</p>
        <Button variant="primary" onClick={() => patch({ onboarding_done: true })}>
          Начать
        </Button>
      </div>
    </div>
  );
}
