import taichi_lang as ti
import numpy as np
import random
import cv2
import matplotlib.pyplot as plt
import time

real = ti.f32
ti.set_default_fp(real)

dim = 2
n_particles = 8192
n_grid = 128
dx = 1 / n_grid
inv_dx = 1 / dx
dt = 3e-4
p_mass = 1
p_vol = 1
E = 100
# TODO: update
mu = E
la = E
max_steps = 1024
steps = 1024
gravity = 9.8
target = [0.3, 0.6]

scalar = lambda: ti.var(dt=real)
vec = lambda: ti.Vector(dim, dt=real)
mat = lambda: ti.Matrix(dim, dim, dt=real)

actuator_id = ti.global_var(ti.i32)
x, v, x_avg = vec(), vec(), vec()
grid_v_in, grid_m_in = vec(), scalar()
grid_v_out = vec()
C, F = mat(), mat()

init_v = vec()
loss = scalar()


# ti.cfg.arch = ti.x86_64
# ti.cfg.use_llvm = True
# ti.cfg.arch = ti.cuda
# ti.cfg.print_ir = True


@ti.layout
def place():
  ti.root.dense(ti.i, n_particles).place(actuator_id)
  ti.root.dense(ti.l, max_steps).dense(ti.k, n_particles).place(x, v, C, F)
  ti.root.dense(ti.ij, n_grid).place(grid_v_in, grid_m_in, grid_v_out)
  ti.root.place(init_v, loss, x_avg)
  
  ti.root.lazy_grad()


@ti.kernel
def set_v():
  for i in range(n_particles):
    v[0, i] = init_v


@ti.kernel
def clear_grid():
  for i, j in grid_m_in:
    grid_v_in[i, j] = [0, 0]
    grid_m_in[i, j] = 0
    grid_v_in.grad[i, j] = [0, 0]
    grid_m_in.grad[i, j] = 0
    grid_v_out.grad[i, j] = [0, 0]


@ti.kernel
def clear_particle_grad():
  # for all time steps and all particles
  for f, i in x:
    x.grad[f, i] = [0, 0]
    v.grad[f, i] = [0, 0]
    C.grad[f, i] = [[0, 0], [0, 0]]
    F.grad[f, i] = [[0, 0], [0, 0]]


@ti.kernel
def p2g(f: ti.i32):
  for p in range(0, n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, ti.i32)
    w = [0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1),
         0.5 * ti.sqr(fx - 0.5)]
    new_F = (ti.Matrix.diag(dim=2, val=1) + dt * C[f, p]) @ F[f, p]
    F[f + 1, p] = new_F
    J = ti.determinant(new_F)
    r, s = ti.polar_decompose(new_F)
    cauchy = 2 * mu * (new_F - r) @ ti.transposed(new_F) + \
             ti.Matrix.diag(2, la * (J - 1) * J)
    stress = -(dt * p_vol * 4 * inv_dx * inv_dx) * cauchy
    affine = stress + p_mass * C[f, p]
    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        offset = ti.Vector([i, j])
        dpos = (ti.cast(ti.Vector([i, j]), real) - fx) * dx
        weight = w[i](0) * w[j](1)
        grid_v_in[base + offset].atomic_add(
          weight * (p_mass * v[f, p] + affine @ dpos))
        grid_m_in[base + offset].atomic_add(weight * p_mass)


bound = 3


@ti.kernel
def grid_op():
  for i, j in grid_m_in:
    inv_m = 1 / (grid_m_in[i, j] + 1e-10)
    v_out = inv_m * grid_v_in[i, j]
    v_out[1] -= dt * gravity
    if i < bound and v_out[0] < 0:
      v_out[0] = 0
    if i > n_grid - bound and v_out[0] > 0:
      v_out[0] = 0
    if j < bound and v_out[1] < 0:
      v_out[1] = 0
    if j > n_grid - bound and v_out[1] > 0:
      v_out[1] = 0
    grid_v_out[i, j] = v_out


@ti.kernel
def g2p(f: ti.i32):
  for p in range(0, n_particles):
    base = ti.cast(x[f, p] * inv_dx - 0.5, ti.i32)
    fx = x[f, p] * inv_dx - ti.cast(base, real)
    w = [0.5 * ti.sqr(1.5 - fx), 0.75 - ti.sqr(fx - 1.0),
         0.5 * ti.sqr(fx - 0.5)]
    new_v = ti.Vector([0.0, 0.0])
    new_C = ti.Matrix([[0.0, 0.0], [0.0, 0.0]])
    
    for i in ti.static(range(3)):
      for j in ti.static(range(3)):
        dpos = ti.cast(ti.Vector([i, j]), real) - fx
        g_v = grid_v_out[base(0) + i, base(1) + j]
        weight = w[i](0) * w[j](1)
        new_v += weight * g_v
        new_C += 4 * weight * ti.outer_product(g_v, dpos) * inv_dx
    
    v[f + 1, p] = new_v
    x[f + 1, p] = x[f, p] + dt * v[f + 1, p]
    C[f + 1, p] = new_C


@ti.kernel
def compute_x_avg():
  for i in range(n_particles):
    x_avg[None].atomic_add((1 / n_particles) * x[steps - 1, i])


@ti.kernel
def compute_loss():
  dist = ti.sqr(x_avg - ti.Vector(target))
  loss[None] = 0.5 * (dist(0) + dist(1))


def forward():
  # simulation
  set_v()
  for s in range(steps - 1):
    clear_grid()
    p2g(s)
    grid_op()
    g2p(s)
  
  loss[None] = 0
  x_avg[None] = [0, 0]
  compute_x_avg()
  compute_loss()
  return loss[None]


def backward():
  clear_particle_grad()
  init_v.grad[None] = [0, 0]
  
  loss.grad[None] = 1
  x_avg.grad[None] = [0, 0]
  
  compute_loss.grad()
  compute_x_avg.grad()
  for s in reversed(range(steps - 1)):
    # Since we do not store the grid history (to save space), we redo p2g and grid op
    clear_grid()
    p2g(s)
    grid_op()
    
    g2p.grad(s)
    grid_op.grad()
    p2g.grad(s)
  set_v.grad()
  return init_v.grad[None]


class Scene:
  def __init__(self):
    self.n_particles = 0
    self.x = []
    self.actuator_id = []
  
  def add_rect(self, x, y, w, h, actuation):
    global n_particles
    w_count = int(w / dx)
    h_count = int(h / dx)
    real_dx = w / w_count
    real_dy = h / h_count
    for i in range(w_count):
      for j in range(h_count):
        self.x.append([x + (i + 0.5) * real_dx, y + (j + 0.5) * real_dy])
        self.actuator_id.append(actuation)
        self.n_particles += 1
  
  def finalize(self):
    global n_particles
    n_particles = self.n_particles


def main():
  # initialization
  scene = Scene()
  scene.add_rect(0.1, 0.1, 0.2, 0.2, -1)
  scene.add_rect(0.1, 0.3, 0.5, 0.2, -1)
  scene.finalize()
  init_v[None] = [0, 0]
  
  for i in range(scene.n_particles):
    x[0, i] = scene.x[i]
    F[0, i] = [[1, 0], [0, 1]]
    actuator_id[i] = scene.actuator_id[i]
  
  losses = []
  img_count = 0
  for i in range(30):
    l = forward()
    losses.append(l)
    grad = backward()
    print('loss=', l, '   grad=', (grad[0], grad[1]))
    learning_rate = 10
    init_v(0)[None] -= learning_rate * grad[0]
    init_v(1)[None] -= learning_rate * grad[1]
    
    # visualize
    for s in range(63, steps, 64):
      scale = 4
      img = np.zeros(shape=(scale * n_grid, scale * n_grid)) + 0.3
      total = [0, 0]
      for i in range(n_particles):
        p_x = int(scale * x(0)[s, i] / dx)
        p_y = int(scale * x(1)[s, i] / dx)
        total[0] += p_x
        total[1] += p_y
        img[p_x, p_y] = 1
      cv2.circle(img, (total[1] // n_particles, total[0] // n_particles),
                 radius=5, color=0, thickness=5)
      cv2.circle(img, (
      int(target[1] * scale * n_grid), int(target[0] * scale * n_grid)),
                 radius=5, color=1, thickness=5)
      img = img.swapaxes(0, 1)[::-1]
      cv2.imshow('MPM', img)
      img_count += 1
      # cv2.imwrite('MPM{:04d}.png'.format(img_count), img * 255)
      cv2.waitKey(1)
    ti.profiler_print()
  
  ti.profiler_print()
  plt.title("Optimization of Initial Velocity")
  plt.ylabel("Loss")
  plt.xlabel("Gradient Descent Iterations")
  plt.plot(losses)
  plt.show()


if __name__ == '__main__':
  main()
