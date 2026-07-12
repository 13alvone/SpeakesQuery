# Vendored third-party frontend assets

Committed to the repo (weakness audit W10, 2026-07-12) so the app runs
fully offline and the README's zero-cloud-dependency claim is grep-proof.
Do not hand-edit these files; to upgrade, replace the whole directory
from the pinned upstream release and update this manifest + ui.html refs.

| Package | Version | Source |
|---|---|---|
| marked | 12.0.2 | https://registry.npmjs.org/marked (npm) |
| codemirror | 5.65.18 | https://registry.npmjs.org/codemirror (npm) |
| monaco-editor | 0.45.0 | https://registry.npmjs.org/monaco-editor (npm tarball, package/min/vs) |
| vega | 5.33.1 | https://registry.npmjs.org/vega (npm) |
| vega-lite | 5.23.0 | https://registry.npmjs.org/vega-lite (npm) |
| vega-embed | 6.29.0 | https://registry.npmjs.org/vega-embed (npm) |

SHA-256 of every vendored file (regenerate: find . -type f ! -name MANIFEST.md -print0 | sort -z | xargs -0 sha256sum):

```
2e2daa9c4680d9415429bd9a51b0e0ac8adf5bc321dcfc43bc36b17a26ee6b33  ./codemirror/addon/comment/comment.min.js
814a8efaea171d27ec681b94bea3936a140b48ff355a572661715da18056edc9  ./codemirror/addon/edit/closebrackets.min.js
3c9e6befa28b77612e4541effc48988137505070cda12ee7644749afb4db070f  ./codemirror/addon/edit/matchbrackets.min.js
9058c1c14fcdae199b490bb6214f36a216b9ce84d7df2084830ebb6a60337651  ./codemirror/addon/hint/show-hint.css
44c871fbe54cdc7dea25bb28a0cb73eb8e311b23dd9e42ddba19b8e21295a746  ./codemirror/addon/hint/show-hint.min.js
badac4549a80b06e7e7b23262622a6c973d89177e3ad66b638d07d5f5df5f0ff  ./codemirror/addon/selection/active-line.min.js
d8fddcfca0ccaeea67ddd557d22340530a1daae8a09843e8f4c25d7def06efa8  ./codemirror/lib/codemirror.min.css
9eb3d93e642327e5f350342a60e6810aa1543644ba003e41bad2f372ead3b372  ./codemirror/lib/codemirror.min.js
213004abf6641e2fd6830eb48a512f016050fe38e6398802391039a7ab7a4a6f  ./codemirror/mode/python/python.min.js
15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894  ./marked/marked.min.js
b3ffc1af5867d6c901ea05f38ee12f8f8369db4912a91d56156ee9c85bd47dc3  ./monaco-editor/min/vs/base/browser/ui/codicons/codicon/codicon.ttf
1ea09d107089dc1e8bc0ba408fefcbdcbf366c697ba216f88da49330130e0514  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.de.js
56341c7827241a6bf388660a020b45e3f5a191b7da46f7a9bc30fbcc61ff2ebb  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.es.js
4a3afc911e223f70f2ffe4febd392fffff6011607cc9752c4313e951121bc36a  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.fr.js
74886ff47cb9ba5dcb72e223887ba3fc91b19f9818aeb9cbfc64a56203f22993  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.it.js
04b190db7bc19af7dd6d28069b0a8fbb2baeedcdbead5356773444049eb2e524  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.ja.js
a7b212e2cd848787a8af48fc99c5ce82dee49a8534de42ff4833024e93ca4d19  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.js
377f5295df6a60b920589743362fa6400e1ec8825bcd0a11d19fe873d6aaef98  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.ko.js
715d1a916bb311ffb62b9114b186d86214c70ce8720589d894859102d002fb37  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.ru.js
cece19ca9db35eb58973a81ec27fc9866759920c2ba789ada2887a94400f4de5  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.zh-cn.js
6d06a8de18319120f905b26e564dda2e2b464359cf565b8bb13154edc9a30d7e  ./monaco-editor/min/vs/base/common/worker/simpleWorker.nls.zh-tw.js
d3acd18994f2571c2511314d049689d1b2d649ba667ee1f7291eb0750c08494f  ./monaco-editor/min/vs/base/worker/workerMain.js
69a2e9c84833412f35627b2681259fafb5602632c86a002bd819592d5280ec3b  ./monaco-editor/min/vs/basic-languages/abap/abap.js
e24fd69a21c6193f82fa3194fd3e2902308b1624150124973ef38c98cf0e5397  ./monaco-editor/min/vs/basic-languages/apex/apex.js
0bcdfd620dadca6ef6b9cd908228790e82b06fbbd6607513789cea42bbf1c67f  ./monaco-editor/min/vs/basic-languages/azcli/azcli.js
64c1d55e14052eb1e56f09de7380274a6cde5f6579de317d6ca7e3d27cc11a18  ./monaco-editor/min/vs/basic-languages/bat/bat.js
138890bb900c772b9cd85a6a880a4c3834d4cf69fe60a7db4f0ed5ac6a6036ca  ./monaco-editor/min/vs/basic-languages/bicep/bicep.js
9c69c0623eaa0c03d7268f7b640e7b1e4a7613168fb7453e1df44d918d20d37c  ./monaco-editor/min/vs/basic-languages/cameligo/cameligo.js
f518edbd296f40149368695cfd50bad56c40d3e98648d4bcb24fb887df0763b6  ./monaco-editor/min/vs/basic-languages/clojure/clojure.js
b71fa40d1dfcf8a0a4a1d9741bdcb480ed427f6449d504039cf0d6da230cd5b9  ./monaco-editor/min/vs/basic-languages/coffee/coffee.js
f6f92abe974f9338086148c8b0a60fb565ae6edd9246b676fa9d0035f4e8d9eb  ./monaco-editor/min/vs/basic-languages/cpp/cpp.js
28a25e9b68e5f0c3af4bd163e1aac554bab89078770dfcaba0a63a4b5b8a2543  ./monaco-editor/min/vs/basic-languages/csharp/csharp.js
082eb55c25cc428c596b69d8024eb8ad5932fd556d47fdb4696124086af3cb28  ./monaco-editor/min/vs/basic-languages/csp/csp.js
ed689dc48b3a5c75d3f4088b87501b28c69ea53d94bd4d195439b9d0b0125240  ./monaco-editor/min/vs/basic-languages/css/css.js
9f9251788f10c3f0bb4eb811ce6a7f5ad8db7bbee057b76a77c77c2421fdaae1  ./monaco-editor/min/vs/basic-languages/cypher/cypher.js
da7ef3dee2fa6414326ff00bfc0417d107d51baedfe57389e59058072a00020c  ./monaco-editor/min/vs/basic-languages/dart/dart.js
5bfe103714ff8153914daa7c1cc066d59dab07ca7388d15098d650149ee3be74  ./monaco-editor/min/vs/basic-languages/dockerfile/dockerfile.js
9ce97ccf4768af38976d051504b59dc37b65f56d8bcb638b97d6a82cda4e1b79  ./monaco-editor/min/vs/basic-languages/ecl/ecl.js
0494ea6b17f3f6e2e088dbc9001aaf77d742d757867263d8b0e5fb432b31a2a5  ./monaco-editor/min/vs/basic-languages/elixir/elixir.js
8eb1690c57d0f458d66e89a57e2aba95e6375ee9fef3e7ab5eca9ffa0b3eab7d  ./monaco-editor/min/vs/basic-languages/flow9/flow9.js
454ce0eb30379a348562cbd10b361ea37dcd787e144f9eabb05f977a01c1996f  ./monaco-editor/min/vs/basic-languages/freemarker2/freemarker2.js
2e6b617140b6d195f890488759776da4071506f27ac5719c1ee2f8ec5ffd0ee5  ./monaco-editor/min/vs/basic-languages/fsharp/fsharp.js
ea2d2e46f02faf4d44b95e07713f5784588a4223c218f75c12820eda16eb1b27  ./monaco-editor/min/vs/basic-languages/go/go.js
c937a21620654373da4d87b9e2f20cbca1c02906d05f53dde5e41fe737dee930  ./monaco-editor/min/vs/basic-languages/graphql/graphql.js
e17ff28cfda2d8306c9e74394d67329a6fa98d696af08f66b9e7b42e87971cc0  ./monaco-editor/min/vs/basic-languages/handlebars/handlebars.js
20e7b4ae616a398d20bf97a25f773df9d4d56c5008717be2f29c21a37f4bdb2b  ./monaco-editor/min/vs/basic-languages/hcl/hcl.js
2213b2419fefec449189ccd7ada6e444fc0ae9325f53d77f27be916dd82ed522  ./monaco-editor/min/vs/basic-languages/html/html.js
3d888e02d0b0ced0ee1464e307d7689653ece70553c17bc4f0325c436f033e9b  ./monaco-editor/min/vs/basic-languages/ini/ini.js
ee57d3885b119a5585180caa2c26498bf6e6640b10481dc9677b7610198c4e2e  ./monaco-editor/min/vs/basic-languages/java/java.js
e7276c9e1382aebb07c75f6f62d52f7df17ddb06eced55cbf2b6f0be6b22b113  ./monaco-editor/min/vs/basic-languages/javascript/javascript.js
763d2d709cc767aebb86d7f47e094a2ff0ec6c2ff20db746087da25f5793cf1b  ./monaco-editor/min/vs/basic-languages/julia/julia.js
e4820fcf63e4c464499aab40ff948027987e1484a045a91741bba480fc71d687  ./monaco-editor/min/vs/basic-languages/kotlin/kotlin.js
291122062f89179db9a9495490fa112aeb003b04cd68e51f34722e79fec05188  ./monaco-editor/min/vs/basic-languages/less/less.js
d55482fd3ffccd1f243c9335dbc10504e0896ea0b27b9e5db8bd129c1aad8dd2  ./monaco-editor/min/vs/basic-languages/lexon/lexon.js
ca01c2b1a36ab94e30542f3b936798b3b8d51c2bfc88789e416c26529e91d785  ./monaco-editor/min/vs/basic-languages/liquid/liquid.js
4475c0f4d02c2c6145b097b80cc7e4c3af9246639d20869ff394fe70926cd942  ./monaco-editor/min/vs/basic-languages/lua/lua.js
bcddb65cfdc3c63e67ccf57993ecea2f279b61f92ad046e096d980a706d23017  ./monaco-editor/min/vs/basic-languages/m3/m3.js
66f8b288ede67f908cb776aeff8c9e24996182b878c5da7ffa4bd6f3cbc38a6b  ./monaco-editor/min/vs/basic-languages/markdown/markdown.js
12e41644e29f943e567e09043ac95f9afe3a12f78221662a7c35561424d30ab6  ./monaco-editor/min/vs/basic-languages/mdx/mdx.js
5ee4997c552f1f6bc4deca08bbe628b9369af09e5a94770be18cd419963d834d  ./monaco-editor/min/vs/basic-languages/mips/mips.js
f53ec8830dba059c6f9ffe5d74ad6556372c927e4465ebeb6c27e1c7f915157d  ./monaco-editor/min/vs/basic-languages/msdax/msdax.js
ca45c24c9eccc351207c790fce382c66ee66e3b74562dec74f3ad306ccf1687a  ./monaco-editor/min/vs/basic-languages/mysql/mysql.js
15b5f02919df2434a0d0305c301cf6450c98e0076fbfee1baaef93d339b2102c  ./monaco-editor/min/vs/basic-languages/objective-c/objective-c.js
11cf852948fc74873e7f58bd88c34e7aff02e3655faa2b75d741fab0e07a1922  ./monaco-editor/min/vs/basic-languages/pascaligo/pascaligo.js
ea9b0cd3df7ba28f7f9b4b484508d402096026c62399995278931cb91b45f3a1  ./monaco-editor/min/vs/basic-languages/pascal/pascal.js
1d5eee661da60fba32173970d751b604fbc0daa160bc4463128ee35ea5e306fe  ./monaco-editor/min/vs/basic-languages/perl/perl.js
354896ae790e785e67e7f0fe20836e49b8650413ebebc0a497e600cd9baebb2a  ./monaco-editor/min/vs/basic-languages/pgsql/pgsql.js
621f1298e5ce5879f6ec3f8ee887b0039d288690f2529752c13c5ba7166caed7  ./monaco-editor/min/vs/basic-languages/php/php.js
65d24fd68d69edbb30b01953c044e3502bac0205e7a9db0447e38562383bdddd  ./monaco-editor/min/vs/basic-languages/pla/pla.js
71e5eb8caaf7112e99c4b7c6befafc1805b47f3460ab520b22a1df07b65f9fc3  ./monaco-editor/min/vs/basic-languages/postiats/postiats.js
6a450d27ddde2ca99a5a38734f11578fe67cc5240ce251931f9454baca45516a  ./monaco-editor/min/vs/basic-languages/powerquery/powerquery.js
4e376a8005c85fb5bdde94f900920a29faaa2735a9ed9ca2f9e28abef231fed0  ./monaco-editor/min/vs/basic-languages/powershell/powershell.js
6e62b15b050502abef7d2f721a2c395a4774bbd43e1e0b0b02da2129ba8f663f  ./monaco-editor/min/vs/basic-languages/protobuf/protobuf.js
7989c74ce5c72ed5559103a4289e3d8c67387d552a12e5495ca186d7d5985960  ./monaco-editor/min/vs/basic-languages/pug/pug.js
34ed9698f52c310a991ed3a9a375d0ba8d86690bbbcb488cf7ed23606ac6ca84  ./monaco-editor/min/vs/basic-languages/python/python.js
85e681adf56a9813ab1a514dddea17b919ed48ae07c4a564703619f93dca0d78  ./monaco-editor/min/vs/basic-languages/qsharp/qsharp.js
b67392d4baefbc48e9f0e28e661add4be183bec81016753cac90d901a382b62f  ./monaco-editor/min/vs/basic-languages/razor/razor.js
805fa3f57140f854aae63c1312187db390206978c922e1527190206db207963a  ./monaco-editor/min/vs/basic-languages/redis/redis.js
b1bd4c4d566b0d0688803f8b13ecc667c299100ab45c3f187b85906446f4eb65  ./monaco-editor/min/vs/basic-languages/redshift/redshift.js
1d05f1659f95d61251b4efb8f9cbbcb06652fa82f95080637fcdbdf20b5b9242  ./monaco-editor/min/vs/basic-languages/restructuredtext/restructuredtext.js
0db30b3566ed90be1d37712cb86e40becb27edbf7ba1207a1487b981bc8e7f61  ./monaco-editor/min/vs/basic-languages/r/r.js
7a8f786527b3546e88c350d9b11de0edc801276abc00741526407dc6859712d9  ./monaco-editor/min/vs/basic-languages/ruby/ruby.js
206151559b889581b3b522026a57f228ceab4fb864747074bd80e7041cdb5beb  ./monaco-editor/min/vs/basic-languages/rust/rust.js
58001ef158de456bb44ebe9e7616308be4f909c99c7483edd4dfb9d886922c41  ./monaco-editor/min/vs/basic-languages/sb/sb.js
fe4369dd35b2fe4879d63699af578f4fc42a40154416f1f4dda6de8fe927d66d  ./monaco-editor/min/vs/basic-languages/scala/scala.js
d42be5d22975e53e1fca77bc2250ecb59125a5c58047292bc20cbce06f5c30d7  ./monaco-editor/min/vs/basic-languages/scheme/scheme.js
52ee58f53cd91a92ed0dba2da45fd2e692604fdb9ea4cb2f86718cc798d76399  ./monaco-editor/min/vs/basic-languages/scss/scss.js
a0d9f1a5cf3a2c1c62563226574240a7064ca8c7eac39d061e87141a7cd8e4b5  ./monaco-editor/min/vs/basic-languages/shell/shell.js
950af3dff2530d4fa5d9daf02a0433b61b0ca72a90abdad76a4774cb6563292c  ./monaco-editor/min/vs/basic-languages/solidity/solidity.js
3a090d3736d241bb706ec8011aa31d0cf3ef13715813eb8f1100b4738b63b025  ./monaco-editor/min/vs/basic-languages/sophia/sophia.js
c22b0378faa2004ac710ce64149d231a9ed8c488b2bf84c222b19a4584c4e442  ./monaco-editor/min/vs/basic-languages/sparql/sparql.js
6ff415adfb27a1fbb87cfa40f0da164e9314dd1dfed87a3de39bf2c98e5ded1f  ./monaco-editor/min/vs/basic-languages/sql/sql.js
2a053bcba1a7d58d2806f9206d643b2cda43bec4f0e7d50ae940a6f4322ea162  ./monaco-editor/min/vs/basic-languages/st/st.js
402daa01303d2866384c42a497aa6a969a4858e4c023c6ced61c9b1e4768312d  ./monaco-editor/min/vs/basic-languages/swift/swift.js
bd18a096fecba5ad476e97e5065ec6efd013ef7fba7f9cc2e874a8b67ea0daf0  ./monaco-editor/min/vs/basic-languages/systemverilog/systemverilog.js
cdf4f3fd5d755a43d3a7f76d22559140bf49751e063a2ce444b818fd60c869f7  ./monaco-editor/min/vs/basic-languages/tcl/tcl.js
49989d7c64d2272ab54bd0d7c5008083cb0a4b1bc9285e672893293b2614b14e  ./monaco-editor/min/vs/basic-languages/twig/twig.js
3d69304a326c73ee980632aa45abd086ee1a6509bdf0f689a38b8f433adcef32  ./monaco-editor/min/vs/basic-languages/typescript/typescript.js
36bd2530715b7d696f719207915a70534051b82eff1f3888091759bf5e6d309d  ./monaco-editor/min/vs/basic-languages/vb/vb.js
175fdfc24f48c096a03509f731ba387f30f9e40ef5b783f7383c88f6cfd961b1  ./monaco-editor/min/vs/basic-languages/wgsl/wgsl.js
05c1ca7a4076664a7b1247bbe10680821989d8646eed1912f9d3c5a8ce8bc817  ./monaco-editor/min/vs/basic-languages/xml/xml.js
040ba1de2263eb7785c9c810d3c8dd0da6b119b02c104da055dc45ab7b20cf72  ./monaco-editor/min/vs/basic-languages/yaml/yaml.js
764a3dde806a7414402e87ce4bf2da4f5bb6f36910d887d5c8ee985c1bdf92bd  ./monaco-editor/min/vs/editor/editor.main.css
a79692b8eabd24e0662310c8505d91ab8b85f17db997e4b8f8838980b25bf6df  ./monaco-editor/min/vs/editor/editor.main.js
c17a7143bfec45615dfe279453207810386cf2ddb1765bb51d5ef7588e431c59  ./monaco-editor/min/vs/editor/editor.main.nls.de.js
7eecec9290d4ce0e6ed84629db0d4f1fa579bf747dafdeaba07b0fcccdbca260  ./monaco-editor/min/vs/editor/editor.main.nls.es.js
40cc5f86d28dac219ad76b3b93217147524e9a586497425fb1c7e8946bdf8d39  ./monaco-editor/min/vs/editor/editor.main.nls.fr.js
4415cba01ef4ecc312385a39f3d6f43ad6c3c8b33acb9cbf0c0b95a3a21b352a  ./monaco-editor/min/vs/editor/editor.main.nls.it.js
7ffa7603cb71b964d6eed22f604c51bbefbe560fcdcd309e2668d74b6082eea3  ./monaco-editor/min/vs/editor/editor.main.nls.ja.js
76882f9f1ca076a810cc44891fc00e6c3922d51d8c2d41c5093b016eeaa401cd  ./monaco-editor/min/vs/editor/editor.main.nls.js
67329ca30fe98668b295dcfd19fcfb8958815e56342e750655d9aeb876878847  ./monaco-editor/min/vs/editor/editor.main.nls.ko.js
e217a9b5823906a09e8ca0204a614319aa03607b54836fd9c616fcbfec8861cd  ./monaco-editor/min/vs/editor/editor.main.nls.ru.js
43f6b0598c4492d89246f357d3dca7cc8bfa6a5e100588c9566bc6f5c8e144df  ./monaco-editor/min/vs/editor/editor.main.nls.zh-cn.js
e3c5fa966e4b589fefc683383bfeb92ecb5684ebb67b0f9ea3d2abe51fa5d083  ./monaco-editor/min/vs/editor/editor.main.nls.zh-tw.js
ccfe978a6ff7ae2044d817f846ff0e157b9c088bad6ed504e257975be5cdbc90  ./monaco-editor/min/vs/language/css/cssMode.js
f81e2adf4a550e3d89038a64ef1bd32c0acca59c7d7732a461e5736a02312859  ./monaco-editor/min/vs/language/css/cssWorker.js
c00b1f2f6065c5022d60d8c46828cb726834e9d3a455bdc89780d41748b3b26c  ./monaco-editor/min/vs/language/html/htmlMode.js
8c8f5bdbdb7b31cd16cdbdffbf99aaedba8790b3016e7156f18d5baa8a33c37e  ./monaco-editor/min/vs/language/html/htmlWorker.js
76ac56f40dc8ff457d7033ea2ea06bbb82ac89efa1b8e8a37609a40cc0e4950d  ./monaco-editor/min/vs/language/json/jsonMode.js
6f33c33a4efe691e75008435430c80bd76f8b88726f69e4c22582a16d6a4a135  ./monaco-editor/min/vs/language/json/jsonWorker.js
6404114a7ebdaf7f7f1aca882219fd6ce140b20675003c6bbce48154ba378053  ./monaco-editor/min/vs/language/typescript/tsMode.js
ca6e2bdb4f8dd9a39e2b9ea3336ab98b3b46df5803053ba658db3e862ceb18b0  ./monaco-editor/min/vs/language/typescript/tsWorker.js
effab18afbb4297a23d9d98be95672e4088c735a0677993c329cf29b48914aed  ./monaco-editor/min/vs/loader.js
12d02acfbe3ec59ef9a37dd4822a2e04e2961b5bbb671bbe661d2221715b99da  ./vega/vega-embed.min.js
58c27358e26f2d319cf62f45bc17a4c8362f08645001df2ec8d341eee4097c7f  ./vega/vega-lite.min.js
463f3db6a40b20e9747b4ed38f37ed0add508838f9141b1cf8366784b07b30c8  ./vega/vega.min.js
```
