/**
 * One note: the notes themselves, then the conversation about them.
 *
 * Opened either with a saved note's id, or with `id=new` and the freshly
 * generated result passed through so the user is not made to wait twice.
 */

import { useEffect, useRef, useState } from "react";
import {
  ActivityIndicator, KeyboardAvoidingView, Platform, Pressable, ScrollView,
  StyleSheet, Text, TextInput, View,
} from "react-native";
import Markdown from "react-native-markdown-display";
import { useLocalSearchParams, useNavigation } from "expo-router";
import * as api from "../../src/api";
import { colourFor, useTheme, type Theme } from "../../src/theme";

type Msg = { role: "user" | "assistant"; content: string; findings?: api.Finding[] };

export default function Note() {
  const { id, payload } = useLocalSearchParams<{ id: string; payload?: string }>();
  const t = useTheme();
  const s = styles(t);
  const nav = useNavigation();
  const scroller = useRef<ScrollView>(null);

  const [note, setNote] = useState<api.SavedNote | api.NotesResult | null>(
    payload ? (JSON.parse(payload) as api.NotesResult) : null,
  );
  const [loading, setLoading] = useState(!payload);
  const [error, setError] = useState("");
  const [session, setSession] = useState<string | undefined>(
    payload ? (JSON.parse(payload) as api.NotesResult).session : undefined,
  );
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [question, setQuestion] = useState("");
  const [thinking, setThinking] = useState("");
  const [saved, setSaved] = useState(id !== "new");

  useEffect(() => {
    if (payload || id === "new") return;
    (async () => {
      try {
        const n = await api.savedNote(id);
        setNote(n); setSession(n.session);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Could not open that note.");
      } finally { setLoading(false); }
    })();
  }, [id, payload]);

  useEffect(() => {
    const subject = note && "subject" in note ? note.subject : undefined;
    nav.setOptions({ title: subject || note?.title || "Notes" });
  }, [note, nav]);

  async function send() {
    const q = question.trim();
    if (!q || thinking) return;
    setQuestion("");
    setMsgs((m) => [...m, { role: "user", content: q }]);
    setThinking("thinking");
    try {
      const { answer } = await api.ask({
        question: q, session,
        notes: note?.notes, title: note?.title,
        history: msgs.slice(-6).map((m) => ({ role: m.role, content: m.content })),
      });
      setMsgs((m) => [...m, { role: "assistant", content: answer }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: e instanceof Error ? e.message : "Failed." }]);
    } finally {
      setThinking("");
      requestAnimationFrame(() => scroller.current?.scrollToEnd({ animated: true }));
    }
  }

  /** Send one claim to the research agent; findings land in the conversation. */
  async function sources(claim: string) {
    if (thinking) return;
    setMsgs((m) => [...m, { role: "user", content: `⌕ ${claim}` }]);
    setThinking("searching");
    try {
      const r = await api.findSources({ claim, session }, setThinking);
      setMsgs((m) => [...m, {
        role: "assistant",
        content: r.verdict || (r.findings.length ? "" : `Read ${r.read} of ${r.searched} candidates; nothing spoke to this directly.`),
        findings: r.findings,
      }]);
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: e instanceof Error ? e.message : "Research failed." }]);
    } finally { setThinking(""); }
  }

  async function saveNote() {
    if (saved || !note) return;
    try {
      await api.save({ session, notes: note.notes, title: note.title });
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save.");
    }
  }

  if (loading) return <View style={s.centre}><ActivityIndicator color={t.brand} /></View>;
  if (error && !note) return <View style={s.centre}><Text style={s.error}>{error}</Text></View>;
  if (!note) return null;

  // Each top-level bullet becomes long-pressable: hold it to hunt for sources.
  const claims = note.notes.split("\n")
    .map((l) => l.trim())
    .filter((l) => /^[-*]\s+/.test(l))
    .map((l) => l.replace(/^[-*]\s+/, "").replace(/\*\*/g, ""))
    .filter((l) => l.length > 24);

  return (
    <KeyboardAvoidingView
      style={{ flex: 1, backgroundColor: t.bgDoc }}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={92}
    >
      <ScrollView ref={scroller} contentContainerStyle={{ padding: 16, paddingBottom: 28 }}>
        <View style={s.meta}>
          {note.platform ? (
            <Text style={[s.tag, { color: colourFor(note.platform) }]}>
              {note.platform.toUpperCase()}
            </Text>
          ) : null}
          <Pressable onPress={saveNote} disabled={saved} style={[s.save, saved && { opacity: 0.45 }]}>
            <Text style={s.saveText}>{saved ? "Saved" : "Save"}</Text>
          </Pressable>
        </View>

        <View style={s.paper}>
          <Markdown style={markdown(t)}>{note.notes}</Markdown>
        </View>

        {claims.length ? (
          <View style={s.claims}>
            <Text style={s.claimsHead}>Check a claim</Text>
            {claims.slice(0, 6).map((c, i) => (
              <Pressable key={i} style={s.claim} onPress={() => sources(c)}>
                <Text style={s.claimText} numberOfLines={2}>{c}</Text>
                <Text style={s.claimGo}>⌕</Text>
              </Pressable>
            ))}
          </View>
        ) : null}

        {msgs.map((m, i) => (
          <View key={i} style={[s.bubble, m.role === "user" ? s.bubbleUser : s.bubbleBot]}>
            {m.content ? (
              m.role === "user"
                ? <Text style={s.userText}>{m.content}</Text>
                : <Markdown style={markdown(t)}>{m.content}</Markdown>
            ) : null}
            {m.findings?.map((f, k) => (
              <View key={k} style={[s.finding, { borderLeftColor: relationColour(f.relation, t) }]}>
                <Text style={[s.relation, { color: relationColour(f.relation, t) }]}>
                  {f.relation.toUpperCase()}
                </Text>
                <Text style={s.findingTitle} numberOfLines={2}>{f.title}</Text>
                <Text style={s.quote}>“{f.quote}”</Text>
                {f.timestamp ? <Text style={s.stamp}>{f.timestamp}</Text> : null}
              </View>
            ))}
          </View>
        ))}

        {thinking ? (
          <View style={[s.bubble, s.bubbleBot, s.thinking]}>
            <ActivityIndicator size="small" color={t.ink3} />
            <Text style={s.thinkingText}>{thinking}…</Text>
          </View>
        ) : null}
      </ScrollView>

      <View style={s.composer}>
        <TextInput
          style={s.ask}
          value={question}
          onChangeText={setQuestion}
          placeholder="Ask about these notes…"
          placeholderTextColor={t.ink3}
          onSubmitEditing={send}
          returnKeyType="send"
          multiline
        />
        <Pressable onPress={send} disabled={!question.trim() || !!thinking}
                   style={[s.send, (!question.trim() || !!thinking) && { opacity: 0.45 }]}>
          <Text style={s.sendText}>Ask</Text>
        </Pressable>
      </View>
    </KeyboardAvoidingView>
  );
}

const relationColour = (r: string, t: Theme) =>
  r === "supports" ? t.ok : r === "disputes" ? t.err : t.brand;

const markdown = (t: Theme) => ({
  body: { color: t.ink, fontSize: 15, lineHeight: 23 },
  heading1: { color: t.ink, fontSize: 21, fontWeight: "700" as const, marginBottom: 6 },
  heading2: { color: t.ink, fontSize: 18, fontWeight: "700" as const, marginTop: 14, marginBottom: 5 },
  heading3: { color: t.ink, fontSize: 15, fontWeight: "700" as const, marginTop: 10 },
  bullet_list: { marginBottom: 8 },
  strong: { fontWeight: "700" as const, backgroundColor: t.highlight },
  code_inline: { backgroundColor: t.surface2, color: t.ink, borderRadius: 4, paddingHorizontal: 4 },
  blockquote: { backgroundColor: t.surface2, borderLeftColor: t.brand, borderLeftWidth: 2, paddingHorizontal: 10 },
});

const styles = (t: Theme) => StyleSheet.create({
  centre: { flex: 1, alignItems: "center", justifyContent: "center", backgroundColor: t.bgDoc },
  error: { color: t.err, fontSize: 14, padding: 24, textAlign: "center" },
  meta: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", marginBottom: 10 },
  tag: { fontSize: 10, fontWeight: "700", letterSpacing: 0.7 },
  save: { paddingHorizontal: 14, paddingVertical: 7, borderRadius: 8, borderWidth: 1, borderColor: t.line, backgroundColor: t.surface },
  saveText: { color: t.ink, fontSize: 13, fontWeight: "600" },
  paper: { backgroundColor: t.surface, borderRadius: 12, borderWidth: 1, borderColor: t.line, padding: 16 },
  claims: { marginTop: 18, gap: 7 },
  claimsHead: { fontSize: 11, color: t.ink3, letterSpacing: 1, textTransform: "uppercase", marginBottom: 2 },
  claim: {
    flexDirection: "row", alignItems: "center", gap: 10,
    backgroundColor: t.surface, borderWidth: 1, borderColor: t.line,
    borderRadius: 9, paddingHorizontal: 12, paddingVertical: 10,
  },
  claimText: { flex: 1, fontSize: 13, color: t.ink2, lineHeight: 18 },
  claimGo: { color: t.brand, fontSize: 15 },
  bubble: { marginTop: 12, borderRadius: 12, padding: 12, maxWidth: "92%" },
  bubbleUser: { alignSelf: "flex-end", backgroundColor: t.brandSoft, borderWidth: 1, borderColor: t.brandLine },
  bubbleBot: { alignSelf: "flex-start", backgroundColor: t.surface, borderWidth: 1, borderColor: t.line },
  userText: { color: t.ink, fontSize: 14, lineHeight: 20 },
  thinking: { flexDirection: "row", alignItems: "center", gap: 8 },
  thinkingText: { color: t.ink3, fontSize: 13 },
  finding: { borderLeftWidth: 3, backgroundColor: t.surface2, borderRadius: 7, padding: 10, marginTop: 8 },
  relation: { fontSize: 9, fontWeight: "700", letterSpacing: 0.8, marginBottom: 3 },
  findingTitle: { fontSize: 13, fontWeight: "600", color: t.ink, marginBottom: 4 },
  quote: { fontSize: 13, fontStyle: "italic", color: t.ink2, lineHeight: 19 },
  stamp: { fontSize: 11, color: t.brand, marginTop: 5 },
  composer: {
    flexDirection: "row", gap: 8, padding: 12, alignItems: "flex-end",
    borderTopWidth: 1, borderTopColor: t.seam, backgroundColor: t.bgChat,
  },
  ask: {
    flex: 1, maxHeight: 120, backgroundColor: t.surface, borderWidth: 1, borderColor: t.line,
    borderRadius: 10, paddingHorizontal: 12, paddingVertical: 10, color: t.ink, fontSize: 15,
  },
  send: { backgroundColor: t.brand, borderRadius: 10, paddingHorizontal: 18, paddingVertical: 12 },
  sendText: { color: "#fff", fontWeight: "600", fontSize: 14 },
});
