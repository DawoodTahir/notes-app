/**
 * Home: one input that takes a link or a topic, and the library below it.
 *
 * The three-pane desktop layout does not translate to a phone, so the panes
 * become a stack: compose at the top, library beneath, and the note itself on
 * its own screen. The deck of saved notes lies flat here — overlapping cards
 * fight touch scrolling.
 */

import { useCallback, useEffect, useState } from "react";
import {
  ActivityIndicator, FlatList, Pressable, RefreshControl, SectionList,
  StyleSheet, Text, TextInput, View,
} from "react-native";
import { useRouter } from "expo-router";
import { useSafeAreaInsets } from "react-native-safe-area-context";
import * as api from "../src/api";
import { colourFor, useTheme, type Theme } from "../src/theme";

const STYLES: { name: string; instruction: string }[] = [
  { name: "Study notes", instruction: "" },
  { name: "Summary", instruction: "A tight summary: a short overview and 5–8 key points. Skip the detailed section." },
  { name: "Arguments", instruction: "Lay out each claim made, the reasoning behind it, and any counterpoint raised. Keep every number, name and citation exactly." },
  { name: "Key quotes", instruction: "Pull the most important verbatim quotes, grouped by theme, each with who said it." },
  { name: "Step by step", instruction: "Present the content as an ordered sequence of steps, in the order they happen." },
  { name: "Flashcards", instruction: "Turn the content into question → answer pairs for revision. One idea per card." },
];

type Section = { title: string; data: api.SavedSummary[] };

export default function Home() {
  const t = useTheme();
  const s = styles(t);
  const router = useRouter();
  const insets = useSafeAreaInsets();

  const [input, setInput] = useState("");
  const [styleIdx, setStyleIdx] = useState(0);
  const [busy, setBusy] = useState(false);
  const [step, setStep] = useState("");
  const [error, setError] = useState("");
  const [hits, setHits] = useState<api.SearchHit[]>([]);
  const [sections, setSections] = useState<Section[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  const loadLibrary = useCallback(async () => {
    try {
      const { notes } = await api.saved();
      const byFolder = new Map<string, api.SavedSummary[]>();
      for (const n of notes) {
        const key = n.category || "Unfiled";
        if (!byFolder.has(key)) byFolder.set(key, []);
        byFolder.get(key)!.push(n);
      }
      setSections([...byFolder].map(([title, data]) => ({ title, data })));
    } catch {
      /* an empty library is not worth an error state */
    }
  }, []);

  useEffect(() => { void loadLibrary(); }, [loadLibrary]);

  async function onSubmit() {
    const raw = input.trim();
    if (!raw || busy) return;
    setError(""); setHits([]);

    // Prose means "find me a video"; a link means "take notes on this one".
    if (!api.looksLikeLink(raw)) {
      setBusy(true); setStep("searching");
      try {
        const { results } = await api.search(raw);
        setHits(results);
        if (!results.length) setError("Nothing found. Try different words.");
      } catch (e) {
        setError(e instanceof Error ? e.message : "Search failed.");
      } finally { setBusy(false); setStep(""); }
      return;
    }

    setBusy(true); setStep("starting");
    try {
      const result = await api.makeNotes(
        { url: raw, extra: STYLES[styleIdx].instruction },
        setStep,
      );
      router.push({ pathname: "/note/[id]", params: { id: "new", payload: JSON.stringify(result) } });
      setInput("");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not generate notes.");
    } finally { setBusy(false); setStep(""); }
  }

  const isLink = api.looksLikeLink(input);

  return (
    <SectionList
      style={{ backgroundColor: t.bgDoc }}
      contentContainerStyle={{ paddingBottom: insets.bottom + 32 }}
      sections={sections}
      keyExtractor={(n) => n.id}
      refreshControl={
        <RefreshControl
          refreshing={refreshing}
          tintColor={t.ink3}
          onRefresh={async () => { setRefreshing(true); await loadLibrary(); setRefreshing(false); }}
        />
      }
      ListHeaderComponent={
        <View style={s.header}>
          <TextInput
            style={s.input}
            value={input}
            onChangeText={setInput}
            placeholder="Paste a link, or describe a topic…"
            placeholderTextColor={t.ink3}
            autoCapitalize="none"
            autoCorrect={false}
            returnKeyType="go"
            onSubmitEditing={onSubmit}
            editable={!busy}
          />
          <Text style={s.hint}>
            {!input.trim() ? " "
              : isLink ? "detected a link — will generate notes"
              : "looks like a topic — will search for videos"}
          </Text>

          <FlatList
            horizontal
            showsHorizontalScrollIndicator={false}
            data={STYLES}
            keyExtractor={(x) => x.name}
            contentContainerStyle={{ gap: 6, paddingVertical: 4 }}
            renderItem={({ item, index }) => (
              <Pressable
                onPress={() => setStyleIdx(index)}
                style={[s.chip, index === styleIdx && s.chipOn]}
              >
                <Text style={[s.chipText, index === styleIdx && s.chipTextOn]}>{item.name}</Text>
              </Pressable>
            )}
          />

          <Pressable
            onPress={onSubmit}
            disabled={busy || !input.trim()}
            style={[s.cta, (busy || !input.trim()) && { opacity: 0.5 }]}
          >
            {busy
              ? <View style={s.ctaBusy}><ActivityIndicator color="#fff" size="small" />
                  <Text style={s.ctaText}>{step || "working"}…</Text></View>
              : <Text style={s.ctaText}>{isLink || !input.trim() ? "Generate notes" : "Search videos"}</Text>}
          </Pressable>

          {error ? <Text style={s.error}>{error}</Text> : null}

          {hits.map((h) => (
            <Pressable key={h.url} style={s.hit} onPress={() => { setInput(h.url); setHits([]); }}>
              <Text style={s.hitTitle} numberOfLines={2}>{h.title}</Text>
              <Text style={s.hitMeta} numberOfLines={1}>
                {[h.uploader, h.duration ? `${Math.round(h.duration / 60)}m` : null]
                  .filter(Boolean).join(" · ")}
              </Text>
            </Pressable>
          ))}
        </View>
      }
      renderSectionHeader={({ section }) => (
        <View style={s.folder}>
          <Text style={s.folderName}>{section.title}</Text>
          <Text style={s.folderCount}>{section.data.length}</Text>
        </View>
      )}
      renderItem={({ item }) => (
        <Pressable
          style={s.card}
          onPress={() => router.push({ pathname: "/note/[id]", params: { id: item.id } })}
        >
          <View style={[s.spine, { backgroundColor: colourFor(item.platform) }]} />
          <View style={s.cardBody}>
            <Text style={s.cardTitle} numberOfLines={2}>{item.label || item.title}</Text>
            {item.platform ? (
              <Text style={[s.cardTag, { color: colourFor(item.platform) }]}>
                {item.platform.toUpperCase()}
              </Text>
            ) : null}
          </View>
        </Pressable>
      )}
      ListEmptyComponent={
        <Text style={s.empty}>Nothing saved yet. Generate notes, then press Save.</Text>
      }
    />
  );
}

const styles = (t: Theme) => StyleSheet.create({
  header: { padding: 16, gap: 10 },
  input: {
    backgroundColor: t.surface, borderColor: t.line, borderWidth: 1, borderRadius: 10,
    paddingHorizontal: 14, paddingVertical: 13, fontSize: 15, color: t.ink,
  },
  hint: { fontSize: 11, color: t.ink3, minHeight: 14 },
  chip: {
    paddingHorizontal: 13, paddingVertical: 7, borderRadius: 100,
    backgroundColor: t.surface, borderWidth: 1, borderColor: t.line,
  },
  chipOn: { backgroundColor: t.brand, borderColor: t.brand },
  chipText: { fontSize: 13, color: t.ink2 },
  chipTextOn: { color: "#fff", fontWeight: "600" },
  cta: {
    backgroundColor: t.brand, borderRadius: 10, paddingVertical: 14,
    alignItems: "center", marginTop: 4,
  },
  ctaBusy: { flexDirection: "row", alignItems: "center", gap: 8 },
  ctaText: { color: "#fff", fontWeight: "600", fontSize: 15 },
  error: { color: t.err, fontSize: 13, lineHeight: 18 },
  hit: {
    backgroundColor: t.surface, borderColor: t.line, borderWidth: 1,
    borderRadius: 9, padding: 11, marginTop: 6,
  },
  hitTitle: { fontSize: 14, fontWeight: "600", color: t.ink, marginBottom: 3 },
  hitMeta: { fontSize: 11, color: t.ink3 },
  folder: {
    flexDirection: "row", justifyContent: "space-between", alignItems: "baseline",
    paddingHorizontal: 16, paddingTop: 18, paddingBottom: 7,
    backgroundColor: t.bgDoc,
  },
  folderName: { fontSize: 15, fontWeight: "700", color: t.ink },
  folderCount: { fontSize: 11, color: t.ink3 },
  card: {
    flexDirection: "row", marginHorizontal: 16, marginBottom: 8,
    backgroundColor: t.surface, borderRadius: 10, overflow: "hidden",
    borderWidth: 1, borderColor: t.line,
  },
  spine: { width: 3 },
  cardBody: { flex: 1, padding: 12, gap: 5 },
  cardTitle: { fontSize: 14, fontWeight: "600", color: t.ink, lineHeight: 19 },
  cardTag: { fontSize: 9, fontWeight: "700", letterSpacing: 0.6 },
  empty: { color: t.ink3, fontSize: 13, textAlign: "center", padding: 28, lineHeight: 19 },
});
