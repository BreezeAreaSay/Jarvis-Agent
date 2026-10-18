// Левая колонка: «+ Новый чат», чаты (закреплённые сверху, голосовые — с микрофоном), меню чата
// (переименовать, закрепить, удалить) по «⋯» и правому клику, внизу — «Настройки Jarvis AI».
import { Ellipsis, Mic, MessagesSquare, Pencil, Pin, PinOff, Plus, RotateCcw, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Chat } from "@/api/types";
import { navigate } from "@/lib/router";
import { Button, cx, Dots, Modal } from "@/ui";
import { Menu } from "./Menu";
import { useAi } from "./store";
import { fmtShort } from "./util";

function ChatItem({
  chat,
  active,
  busy,
  onMenu,
  renaming,
  onRenamed,
}: {
  chat: Chat;
  active: boolean;
  busy: boolean;
  onMenu: (chat: Chat, at: { anchor?: HTMLElement; point?: { x: number; y: number } }) => void;
  renaming: boolean;
  onRenamed: () => void;
}) {
  const rename = useAi((s) => s.rename);
  const [title, setTitle] = useState(chat.title);
  const input = useRef<HTMLInputElement>(null);
  const moreBtn = useRef<HTMLButtonElement>(null);
  const done = useRef(false);
  useEffect(() => {
    if (renaming) {
      done.current = false;
      setTitle(chat.title);
      requestAnimationFrame(() => input.current?.select());
    }
  }, [renaming, chat.title]);

  if (renaming) {
    const commit = () => {
      if (done.current) return;
      done.current = true;
      rename(chat.id, title);
      onRenamed();
    };
    return (
      <div className="ai-chat-item renaming">
        <input
          ref={input}
          className="ai-rename"
          value={title}
          maxLength={80}
          aria-label="Название чата"
          onChange={(e) => setTitle(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") commit();
            if (e.key === "Escape") {
              done.current = true;
              onRenamed();
            }
          }}
        />
      </div>
    );
  }

  return (
    <div
      className={cx("ai-chat-item", active && "active", !!chat.pinned && "pinned")}
      onContextMenu={(e) => {
        e.preventDefault();
        onMenu(chat, { point: { x: e.clientX, y: e.clientY } });
      }}
    >
      <button
        type="button"
        className="ai-chat-open"
        onClick={() => navigate("ai", chat.id)}
        aria-current={active ? "page" : undefined}
        title={chat.title}
      >
        {chat.kind === "voice" && <Mic className="kind" aria-label="Голосовой чат" />}
        <span className="ellipsis grow">{chat.title || "Без названия"}</span>
        {busy ? (
          <span className="meta busy" aria-label="Отвечает">
            <Dots />
          </span>
        ) : (
          <span className="meta">
            {!!chat.pinned && <Pin className="pin" aria-label="Закреплён" />}
            <span className="time">{fmtShort(chat.updated)}</span>
          </span>
        )}
      </button>
      <button
        ref={moreBtn}
        type="button"
        className="ai-chat-more"
        aria-label={`Действия с чатом «${chat.title}»`}
        aria-haspopup="menu"
        onClick={() => onMenu(chat, { anchor: moreBtn.current ?? undefined })}
      >
        <Ellipsis />
      </button>
    </div>
  );
}

export function ChatList({ activeId }: { activeId: string | null }) {
  const chats = useAi((s) => s.chats);
  const loaded = useAi((s) => s.chatsLoaded);
  const error = useAi((s) => s.chatsError);
  const threads = useAi((s) => s.threads);
  const loadChats = useAi((s) => s.loadChats);
  const newChat = useAi((s) => s.newChat);
  const setPinned = useAi((s) => s.setPinned);
  const remove = useAi((s) => s.remove);
  const [menu, setMenu] = useState<{ chat: Chat; anchor?: HTMLElement; point?: { x: number; y: number } } | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Chat | null>(null);
  const [deleting, setDeleting] = useState(false);

  const doDelete = async () => {
    if (!confirmDelete) return;
    setDeleting(true);
    await remove(confirmDelete.id);
    setDeleting(false);
    if (confirmDelete.id === activeId) navigate("ai", "new");
    setConfirmDelete(null);
  };

  return (
    <aside className="ai-side" aria-label="Чаты">
      <Button variant="primary" block icon={<Plus />} onClick={newChat} className="ai-new">
        Новый чат
      </Button>
      <nav className="ai-chat-list">
        {!loaded &&
          [70, 55, 82, 64].map((w, i) => (
            <div key={i} className="ai-skel" style={{ width: `${w}%` }} />
          ))}
        {loaded && error && (
          <div className="ai-side-note">
            Не загрузил чаты: {error}
            <button type="button" className="ai-link" onClick={loadChats}>
              <RotateCcw /> Повторить
            </button>
          </div>
        )}
        {loaded && !error && chats.length === 0 && (
          <div className="ai-side-note">
            <MessagesSquare />
            Здесь появятся ваши разговоры с Jarvis
          </div>
        )}
        {chats.map((c) => (
          <ChatItem
            key={c.id}
            chat={c}
            active={c.id === activeId}
            busy={!!threads[c.id]?.pending}
            renaming={renaming === c.id}
            onRenamed={() => setRenaming(null)}
            onMenu={(chat, at) => setMenu({ chat, ...at })}
          />
        ))}
      </nav>
      <Button block icon={<SlidersHorizontal />} onClick={() => navigate("settings", "ai")} className="ai-settings-btn">
        Настройки Jarvis AI
      </Button>

      {menu && (
        <Menu
          anchor={menu.anchor}
          point={menu.point}
          placement="bottom-end"
          width={210}
          label="Действия с чатом"
          onClose={() => setMenu(null)}
          items={[
            { key: "rename", label: "Переименовать", icon: <Pencil />, onSelect: () => setRenaming(menu.chat.id) },
            {
              key: "pin",
              label: menu.chat.pinned ? "Открепить" : "Закрепить",
              icon: menu.chat.pinned ? <PinOff /> : <Pin />,
              onSelect: () => setPinned(menu.chat.id, !menu.chat.pinned),
            },
            {
              key: "delete",
              label: "Удалить",
              icon: <Trash2 />,
              danger: true,
              onSelect: () => setConfirmDelete(menu.chat),
            },
          ]}
        />
      )}
      <Modal
        open={!!confirmDelete}
        onClose={() => setConfirmDelete(null)}
        title="Удалить чат?"
        width={420}
        footer={
          <>
            <Button onClick={() => setConfirmDelete(null)}>Отмена</Button>
            <Button variant="danger" icon={<Trash2 />} onClick={doDelete} loading={deleting}>
              Удалить
            </Button>
          </>
        }
      >
        <p className="muted" style={{ margin: 0 }}>
          «{confirmDelete?.title}» и все сообщения в нём пропадут. Это не отменить.
        </p>
      </Modal>
    </aside>
  );
}
