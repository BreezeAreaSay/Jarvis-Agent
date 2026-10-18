// «Синтез речи» — «Выберите голос»: волна голоса, карточки движков, голоса piper на этом ПК.
import { HardDriveDownload, MessageSquare } from "lucide-react";
import { Card } from "@/ui";
import { ModelRow, Note, useSettingsPatch, useVoiceModels, Waveform } from "../kit";
import { TtsEngines } from "../tts";

function PiperModelsCard() {
  const [settings] = useSettingsPatch();
  const models = useVoiceModels();
  const list = models.items.filter((m) => m.kind === "tts");
  if (!settings) return null;
  return (
    <Card
      icon={<HardDriveDownload />}
      title="Голоса Джарвиса на этом ПК"
      sub="Локальные голоса piper: скачиваются один раз и работают без интернета"
    >
      {models.missing ? (
        <Note>Список голосов появится, когда голосовой модуль ядра будет готов.</Note>
      ) : !models.data ? (
        <div className="row faint small" style={{ padding: 10 }}>
          <span className="spinner" /> Загружаю…
        </div>
      ) : (
        <div className="model-list">
          {list.map((m) => {
            const voice = m.id.replace(/^piper-/, "");
            const active = settings.tts.engine === "piper" && settings.tts.piper_voice === voice;
            return (
              <ModelRow
                key={m.id}
                model={{ ...m, required: m.required || active }}
                icon={<MessageSquare />}
                onDownload={() => void models.download(m.id)}
                onRemove={active ? undefined : () => void models.remove(m.id)}
              />
            );
          })}
        </div>
      )}
    </Card>
  );
}

export default function TtsSection() {
  return (
    <div className="stack">
      <div className="set-hero tts-hero">
        <div className="eyebrow">Синтез речи</div>
        <h2 className="hero-title">Выберите голос</h2>
        <Waveform bars={58} height={58} />
      </div>
      <TtsEngines />
      <PiperModelsCard />
    </div>
  );
}
