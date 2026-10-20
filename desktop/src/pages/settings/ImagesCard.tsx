// «Рисование картинок» в настройках Jarvis AI: «сгенерируй картинку…» в чате рисует OpenAI gpt-image-1 по ключу
// человека. Провайдер ИИ — OpenAI с ключом: тот же ключ; иначе — отдельный ключ OpenAI (он же — у голосов OpenAI).
import { ExternalLink, Palette } from "lucide-react";
import { bridge } from "@/lib/bridge";
import { useApp } from "@/store/app";
import { Button, Card } from "@/ui";
import { SecretField } from "./kit";

export function ImagesCard() {
  const provider = useApp((s) => s.settings?.ai.provider ?? "");
  const secrets = useApp((s) => s.secrets);
  const shared = provider === "openai" && !!secrets.ai_api_key;
  const ready = shared || !!secrets.openai_api_key;
  return (
    <Card
      icon={<Palette />}
      title="Рисование картинок"
      sub="«Сгенерируй картинку: кот-астронавт» — в чате за ~30 секунд, около $0,04 за картинку (OpenAI gpt-image-1)"
    >
      <div className="tts-key">
        {shared ? (
          <span className="chip ok">Используется ключ Jarvis AI (OpenAI)</span>
        ) : (
          <>
            <span className="field-label">Ключ OpenAI API — тот же, что для голосов OpenAI</span>
            <SecretField name="openai_api_key" placeholder="sk-…" />
          </>
        )}
      </div>
      {!ready && (
        <p className="muted" style={{ margin: "10px 0 0", fontSize: 12.5 }}>
          Подписка ChatGPT картинки через API не рисует — нужен ключ с platform.openai.com. Своё фото можно приложить
          к сообщению кнопкой «+» — Jarvis нарисует по нему.
        </p>
      )}
      {!ready && (
        <div style={{ marginTop: 10 }}>
          <Button size="sm" variant="ghost" icon={<ExternalLink />} onClick={() => bridge.openExternal("https://platform.openai.com/api-keys")}>
            Получить ключ OpenAI
          </Button>
        </div>
      )}
    </Card>
  );
}
