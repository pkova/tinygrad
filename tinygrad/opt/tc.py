import math
from dataclasses import dataclass
from tinygrad.dtype import DType, dtypes
from tinygrad.helpers import getenv

@dataclass(frozen=True)
class TensorCore: # D = A * B + C, A is (M x K), B is (K x N), C and D are (M x N)
  dims: tuple[int,int,int] # N, M, K
  threads: int # number of threads that construct the warp
  elements_per_thread: tuple[int, int, int] # elements per-thread to load/store from A/B/C
  dtype_in: DType # dtype for A and B
  dtype_out: DType # dtype for C and D
  opts: tuple[str, ...] # ordered tuple of "ux" or "lx" specifying kernel opts to perform. "ux" upcasts dim x and "lx" localizes dim x
  # (local_swizzle, upcast_swizzle, reduce_swizzle)
  # l<num> is the num axis of the locals, similar for u<num> and upcasts, r<num> and reduces
  swizzle: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]
  def n_swizzle(self) -> tuple[tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]], ...]:
    local_axes, upcast_axes, reduce_axes = len(self.get_local_axes()), len(self.get_upcast_axes()), len(self.get_reduce_axes())
    fwd_st = [f"l{i}" for i in range(local_axes)] + [f"r{i}" for i in range(reduce_axes)] + [f"u{i}" for i in range(upcast_axes)]
    st = {s:i for i,s in enumerate(fwd_st)}
    return tuple((tuple([st[x] for x in s[0]]), tuple([st[x] for x in s[1]]), tuple([st[x] for x in s[2]])) for s in self.swizzle)
  def get_reduce_axes(self): return [(i, 2) for i in range(int(math.log2(self.dims[2])))]
  def get_upcast_axes(self): return [opt for opt in self.opts if opt[0] == "u"]
  def get_local_axes(self): return [opt for opt in self.opts if opt[0] == "l"]
  def __str__(self): return "_".join(["WMMA"] + list(map(str, self.dims)) + [self.dtype_in.name, self.dtype_out.name])
  def __post_init__(self):
    # all axes have size 2, <local> <reduce> <upcast> is the order
    local_axes, upcast_axes, reduce_axes = len(self.get_local_axes()), len(self.get_upcast_axes()), len(self.get_reduce_axes())
    assert self.dims[0] * self.dims[1] == 2**(local_axes + upcast_axes), \
      f"N({self.dims[0]}) x M({self.dims[1]}) != local({2**local_axes}) x upcast({2**upcast_axes}) with opts({self.opts})"
    assert 2**local_axes == self.threads, f"{self.threads} threads construct the warp but found {2**local_axes} in {self.opts}"
    assert 2**upcast_axes == self.elements_per_thread[2], \
      f"{self.elements_per_thread[2]} elements from C are processed per thread but found {2**upcast_axes} in {self.opts}"
    # check swizzle
    assert len(self.swizzle[0]) == 3 and len(self.swizzle[1]) == 3, "swizzle has wrong part count"
    assert len(self.swizzle[0][0]) == len(self.swizzle[1][0]) == local_axes, "local swizzle size is wrong"
    assert len(self.swizzle[0][1]) == len(self.swizzle[1][1]) == upcast_axes, "upcast swizzle size is wrong"
    assert len(self.swizzle[0][2]) == len(self.swizzle[1][2]) == reduce_axes, "reduce swizzle size is wrong"
    assert all(sorted(s[0]+s[1]+s[2]) == list(range(local_axes+upcast_axes+reduce_axes)) for s in self.n_swizzle()), "swizzle missing some dims"

# ***** NVIDIA *****

cuda_tc_opts = ("u0","l0","l0","l1","l1","l1","u1")  # shared by all shapes with M=16 N=8

# https://docs.nvidia.com/cuda/parallel-thread-execution/#warp-level-matrix-multiply-accumulate-instructions
cuda_81616 = [TensorCore(dims=(8,16,16), threads=32, elements_per_thread=(8,4,4), dtype_in=di, dtype_out=do, opts=cuda_tc_opts,
  swizzle=((('r1', 'r2', 'l2', 'l3', 'l4'), ('u1', 'r3'), ('l0', 'l1', 'u0', 'r0')),
           (('r1', 'r2', 'u0', 'l0', 'l1'), ('r0', 'r3'), ('l2', 'l3', 'l4', 'u1'))))
  for di,do in [(dtypes.half,dtypes.float), (dtypes.bfloat16,dtypes.float), (dtypes.half,dtypes.half)]]
cuda_8168_f16 = [TensorCore(dims=(8,16,8), threads=32, elements_per_thread=(4,2,4), dtype_in=di, dtype_out=do, opts=cuda_tc_opts,
  swizzle=((('r1', 'r2', 'l2', 'l3', 'l4'), ('r0', 'u1'), ('l0', 'l1', 'u0')),
           (('r1', 'r2', 'u0', 'l0', 'l1'), ('u1', 'r0'), ('l2', 'l3', 'l4'))))
  for di,do in [(dtypes.half,dtypes.float), (dtypes.half,dtypes.half)]]
cuda_8168_tf32 = [TensorCore(dims=(8,16,8), threads=32, elements_per_thread=(4,2,4), dtype_in=dtypes.float, dtype_out=dtypes.float, opts=cuda_tc_opts,
  swizzle=((('r0', 'r1', 'l2', 'l3', 'l4'), ('u1', 'r2'), ('l0', 'l1', 'u0')),
           (('r0', 'r1', 'u0', 'l0', 'l1'), ('u1', 'r2'), ('l2', 'l3', 'l4'))))]
cuda_sm80: list[TensorCore] = cuda_81616 + cuda_8168_f16
if getenv("ALLOW_TF32", 0): cuda_sm80 += cuda_8168_tf32
cuda_sm75: list[TensorCore] = cuda_8168_f16

# ***** AMD *****

# https://gpuopen.com/learn/wmma_on_rdna3/
amd_rdna3 = [TensorCore(dims=(16,16,16), threads=32, elements_per_thread=(16,16,8), dtype_in=di, dtype_out=do,
  opts=("l0","l0","l0","l0","l1","u1","u1","u1"), swizzle=((('l4', 'u0', 'u1', 'u2', 'l0'), ('r1', 'r2', 'r3'), ('l1', 'l2', 'l3', 'r0')),
                                                           (('l0', 'l1', 'l2', 'l3', 'l4'), ('r1', 'r2', 'r3'), ('u0', 'u1', 'u2', 'r0'))))
  for di,do in [(dtypes.half,dtypes.float),(dtypes.half,dtypes.half),(dtypes.bfloat16,dtypes.float)]]
amd_rdna4 = [TensorCore(dims=(16,16,16), threads=32, elements_per_thread=(8,8,8), dtype_in=di, dtype_out=do,
  opts=("l0","l0","l0","l0","u1","u1","u1","l1"), swizzle=((('u0', 'u1', 'u2', 'l4', 'r2'), ('r0', 'r1', 'r3'), ('l0', 'l1', 'l2', 'l3')),
                                                           (('l0', 'l1', 'l2', 'l3', 'r2'), ('r0', 'r1', 'r3'), ('l4', 'u0', 'u1', 'u2'))))
  for di,do in [(dtypes.half,dtypes.float),(dtypes.half,dtypes.half),(dtypes.bfloat16,dtypes.float),(dtypes.bfloat16,dtypes.bfloat16)]]

# https://gpuopen.com/learn/amd-lab-notes/amd-lab-notes-matrix-cores-readme
amd_cdna = [TensorCore(dims=(16,16,16), threads=64, elements_per_thread=(4,4,4), dtype_in=di, dtype_out=do,
  opts=("l0","l0","l0","l0","u1","u1","l1","l1"),
  swizzle=((('u0', 'u1', 'l4', 'l5', 'r2', 'r3'), ('r0', 'r1'), ('l0', 'l1', 'l2', 'l3')),
           (('l0', 'l1', 'l2', 'l3', 'r2', 'r3'), ('r0', 'r1'), ('l4', 'l5', 'u0', 'u1'))))
  for di,do in [(dtypes.half,dtypes.float),(dtypes.bfloat16,dtypes.float)]]

# ***** Apple Metal *****

metal = [TensorCore(dims=(8,8,8), threads=32, elements_per_thread=(2,2,2), dtype_in=di, dtype_out=do,
  opts=("u0","l0","l1","l1","l0","l1"),
  swizzle=((('r1', 'l1', 'l2', 'r2', 'l4'), ('r0',), ('u0', 'l0', 'l3')),
           (('l0', 'r0', 'r1', 'l3', 'r2'), ('u0',), ('l1', 'l2', 'l4'))))
  for di,do in [(dtypes.float,dtypes.float),(dtypes.half,dtypes.float),
                (dtypes.half,dtypes.half),(dtypes.bfloat16,dtypes.float),(dtypes.bfloat16,dtypes.bfloat16)]]

# ***** Apple AMX *****

amx = [TensorCore(dims=(sz,sz,1), threads=1, elements_per_thread=(sz,sz,sz*sz), dtype_in=dt, dtype_out=dt,
                  swizzle=(((), ('u0', 'u1', 'u2', 'u3', 'u4', 'u5', 'u6', 'u7'), ()),
                           ((), ('u4', 'u5', 'u6', 'u7', 'u0', 'u1', 'u2', 'u3'), ())),
                  opts=("u0","u0","u0","u0","u1","u1","u1","u1")) for dt,sz in [(dt, 64 // dt.itemsize) for dt in [dtypes.float]]]

# ***** Intel ****

intel = [TensorCore(dims=(8,8,16), threads=8, elements_per_thread=(16,16,8), dtype_in=dtypes.half, dtype_out=dtypes.float,
                    opts=("l0","l0","l0","u1","u1","u1"),
                    swizzle=((('r1', 'r2', 'r3'), ('u0', 'u1', 'u2'), ('l0', 'l1', 'l2', 'r0')),
                             (('l0', 'l1', 'l2'), ('r1', 'r2', 'r3'), ('u0', 'u1', 'u2', 'r0'))))]
