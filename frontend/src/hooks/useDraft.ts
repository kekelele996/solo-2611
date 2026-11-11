import { useEffect, useRef, useState } from "react";

/**
 * 本地编辑稿：服务端值变化时只在「本地没有未保存修改」的前提下同步，
 * 避免轮询/事件刷新把正在输入的内容冲掉。
 */
export function useDraft(server: string | undefined): {
  text: string;
  setText: (value: string) => void;
  dirty: boolean;
} {
  const [text, setText] = useState(server ?? "");
  const base = useRef(server ?? "");
  useEffect(() => {
    if (server === undefined) return;
    const previous = base.current;
    base.current = server;
    setText((current) => (current === previous ? server : current));
  }, [server]);
  return { text, setText, dirty: server !== undefined && text !== server };
}
