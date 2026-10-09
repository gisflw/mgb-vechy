# Synthetic terrain reference

`terrain.json` preserves 26 focused cases from the scientific source/tests at
commit `0e29ede1d2fbb729cbdffebb2eb5b13bebf231c0`, before Python removal.
These are reference data for the later `mgb::prepro` terrain implementation.
The current Rust tests check readability and coverage, not scientific parity.

Cases contain named operations, input grids, scientific settings, and expected
values or error categories. Grid arrays are row-major; `null` represents invalid
or nodata cells. Six-element transforms use affine `(a,b,c,d,e,f)` order:
`x=a*column+b*row+c`, `y=d*column+e*row+f`. Pixel centers use half-cell offsets.
This differs from GDAL geotransform ordering.

The cases cover AGREE's pixel distance and confinement, raw-DEM HAND, valley
breaches, flats, multiple drainages, owner confinement, rectangular pixels, D8
terminal normalization, domain exits, cycles, and missing directions. Direction
codes are 0 for terminals and 1–8 for N, NE, E, SE, S, SW, W, NW.

Routing directions and products were captured from the reference implementation.
Routing `rank` is historical diagnostic information, not a required Rust API or
output. The 14 LTND cases use independent per-route geodesic sums on the source
CRS ellipsoid, including geographic degrees, grads, projected metres, and feet.
Their comparison tolerance is `rtol=1e-10, atol=1e-7` metres.

Restore focused scientific tests as each operation is implemented. Keep the
historical source/tests available for behavioral details absent from these grids.
