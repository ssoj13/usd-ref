# usd-ref

Curated collection of OpenUSD reference scenes, production assets, and test repositories (linked as shallow git submodules) for testing USD composition, sublayers, overrides, variants, payloads, `timeSamples` animation, and `UsdSkel`.

## Submodules

| Path | Upstream | Notes |
|---|---|---|
| `ALab` | `DigitalProductionExampleLibrary/ALab` | Animal Logic / ASWF production scene (300+ assets, sublayers, overrides, animation, shaders) |
| `caldera` | `Activision/caldera` | Call of Duty: Warzone Caldera (~4 GB, millions of meshes, time-sampled movement) |
| `usd-wg-assets` | `usd-wg/assets` | ASWF USD Working Group reference assets (`StandardShaderBall`, `Teapot`, `ElephantWithMonochord` with `UsdSkel`) |
| `sample_usd` | `ft-lab/sample_usd` | Human-readable `.usda` samples (layers, references, payloads, `jointTest.usda`) |
| `LearnOpenUSD` | `NVIDIA-Omniverse/LearnOpenUSD` | NVIDIA LearnOpenUSD curriculum & scenes (Git LFS) |
| `OpenUSD-Code-Samples` | `NVIDIA-Omniverse/OpenUSD-Code-Samples` | Modular `.usda` + Python/C++ composition & material samples |
| `OpenUSD-plugin-samples` | `NVIDIA-Omniverse/OpenUSD-plugin-samples` | Schema extensions & Hydra 2 scene index samples |
| `OpenUSD` | `PixarAnimationStudios/OpenUSD` | Upstream OpenUSD (`extras/usd/tutorials/animatedTop/`, `pxr/usd/usdSkel/test/`) |

## External Archives (non-Git)

* **Pixar Kitchen Set / City Set / UsdSkel Examples:** <https://openusd.org/release/dl_downloads.html#assets>
* **Intel 4004 Moore Lane (~7.7 GB):** <https://dpel.aswf.io/4004/>
* **ASWF DPEL (SoleMates, OpenPBRShaderPlayground, MaterialXTeapotLion):** <https://dpel.aswf.io/>
* **Apple AR Quick Look USDZ Gallery:** <https://developer.apple.com/augmented-reality/quick-look/>

## Quick Setup

```powershell
git clone --recurse-submodules --shallow-submodules https://github.com/ssoj13/usd-ref.git
```
