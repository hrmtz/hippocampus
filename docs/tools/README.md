# 音律インタラクティブ教材 (music theory tools)

2026-07-04 の音律セッションで構築した自己完結 HTML 教材群。
canonical wiki の music ページ群 (axioms-octave-to-twelve /
temperament-incommensurability / music-pitch-generators) からリンクされている。

| file | deployed artifact | 内容 |
|---|---|---|
| `octave-circle.html` | https://claude.ai/code/artifact/2b49ce89-4c69-4b5b-a263-f2729c9a3c1a | オクターブ等価性を耳と目で。440/880 の実音、log₂ 螺旋 (A1–A7) → ピッチクラス円への射影、同音名=同光線 |
| `harmonic-scope.html` | https://claude.ai/code/artifact/f0fb0a5c-550f-4d64-9ecc-3003ecb4c7b2 | うなり/協和の波形・スペクトル・実音。A=442 弦五度調弦モード (D/G/E 弦、ゼロビート目盛り) |
| `fifths-scale-builder.html` | https://claude.ai/code/artifact/5ce3dfec-c685-4220-9da4-311533609d23 | 五度を上下に積んで音階を建てる。旋法プリセット、鎖由来の正書法 (1周後=B♯)、ウルフ、コンマ試聴 |
| `temperament-rose.html` | https://claude.ai/code/artifact/7f641ac3-5910-413f-b942-0460df97af7a | ピタゴラス/純正律/平均律/Werckmeister III のロゼット重ね比較、調めぐり (♯/♭回り、ET と A/B) |

- 完全自己完結 (外部依存ゼロ、CSP 対応)。ブラウザで直接開いても動く。
- 再デプロイ: 編集 → 同 URL に Artifact publish (URL は不変)。
- code-review (26 agents, 2026-07-04) 済み: 多重再生/エイリアス/綴り等 10 件修正済。
