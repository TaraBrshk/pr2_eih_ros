import time
import numpy as np

import roslib
roslib.load_manifest('tfx')
import tfx
import tf.transformations as tft

import geometry2d
import utils

import IPython

epsilon = 1e-5

class Beam:
    def __init__(self, base, a, b, c, d):
        """
        A pyramid with orign base and points a,b,c,d arranged as
        
        b --- a
        |     |
        |     |
        c --- d
        """
        self.base = np.array(base)
        self.a = np.array(a)
        self.b = np.array(b)
        self.c = np.array(c)
        self.d = np.array(d)
        
    def is_inside(self, p):
        """
        :param p: 3d point as list or np.array
        :return True if p is inside the beam, else False
        """
        p = np.array(p)
        
        halfspaces = self.get_halfspaces()
        return np.min([h.contains(p) for h in halfspaces])    
    
    def get_halfspaces(self):
        """
        :return list of halfspaces representing outward-pointing faces
        """
        base, a, b, c, d = self.base, self.a, self.b, self.c, self.d

        origins = [(base+a+d)/3.0,
                   (base+b+a)/3.0,
                   (base+c+b)/3.0,
                   (base+d+c)/3.0,
                   (a+b+c+d)/4.0]
    
        normals = [-np.cross(a-base, d-base),
                   -np.cross(b-base, a-base),
                   -np.cross(c-base, b-base),
                   -np.cross(d-base, c-base),
                   -np.cross(b-a, d-a)]
        normals = [n/np.linalg.norm(n) for n in normals]
        
        return [Halfspace(origin, normal) for origin, normal in zip(origins, normals)]
        
    def get_side(self, side):
        """
        :param side: 'right', 'top', 'left', 'bottom', 'front'
        :return list of triangles
        """
        if side == 'right':
            return [Triangle(self.base, self.a, self.d)]
        elif side == 'top':
            return [Triangle(self.base, self.a, self.b)]
        elif side == 'left':
            return [Triangle(self.base, self.b, self.c)]
        elif side == 'bottom':
            return [Triangle(self.base, self.c, self.d)]
        elif side == 'front':
            return [Triangle(self.a, self.b, self.c), Triangle(self.a, self.d, self.c)]
        else:
            return None
    
    def plot(self, sim, with_sides=True, color=(1,0,0)):
        """
        Plots edges of the beam
        
        :param sim: Simulator instance
        :param with_sides: if True, plots side edges too
        :param color: (r,g,b) [0,1]
        """
        base, a, b, c, d = self.base, self.a, self.b, self.c, self.d
        
        if with_sides:
            sim.plot_segment(base, a)
            sim.plot_segment(base, b)
            sim.plot_segment(base, c)
            sim.plot_segment(base, d)
        
        sim.plot_segment(a, b)
        sim.plot_segment(b, c)
        sim.plot_segment(c, d)
        sim.plot_segment(d, a)
        
class Halfspace:
    def __init__(self, origin, normal):
        self.origin = origin
        self.normal = normal
        
    def contains(self, x):
        """
        :param x: 3d point as list or np.array
        :return True if x forms acute angle with plane normal, else False
        """
        return np.dot(self.normal, np.array(x) - self.origin) >= epsilon
    
    def plot(self, sim, color=(0,0,1)):
        """
        Plots the normal
        
        :param sim: Simulator instance
        :param color: (r,g,b) [0,1]
        """
        o, n = self.origin, self.normal
        sim.plot_segment(o, o + .05*n, color=color)

class Triangle:
    def __init__(self, a, b, c):
        self.a, self.b, self.c = np.array(a), np.array(b), np.array(c)
        
    def align_with(self, target):
        """
        Aligns the normal of this triangle to target
        
        :param target: 3d list or np.array
        :return (rotated triangle, rotation matrix)
        """
        target = np.array(target)
        source = np.cross(self.b - self.a, self.c - self.a)
        source /= np.linalg.norm(source)
    
        rotation = np.eye(3)
        
        dot = np.dot(source, target)
        if not np.isnan(dot):
            angle = np.arccos(dot)
            if not np.isnan(angle):
                cross = np.cross(source, target)
                cross_norm = np.linalg.norm(cross)
                if not np.isnan(cross_norm) and not cross_norm < epsilon:
                    cross = cross / cross_norm
                    rotation = tft.rotation_matrix(angle, cross)[:3,:3]

        return (Triangle(np.dot(rotation, self.a),
                        np.dot(rotation, self.b),
                        np.dot(rotation, self.c)),
                rotation)
        
    def closest_point_to(self, p):
        """
        Find distance to point p
        by rotating and projecting
        then return that closest point unrotated
        
        :param p: 3d list or np.array
        :return 3d np.array of closest point
        """
        p = np.array(p)
        # align with z-axis so all triangle have same z-coord
        tri_rot, rot = self.align_with([0,0,1])
        tri_rot_z = tri_rot.a[-1]
        p_rot = np.dot(rot, p)
        
        p_2d = p_rot[:2]
        tri_2d = geometry2d.Triangle(tri_rot.a[:2], tri_rot.b[:2], tri_rot.c[:2])
        
        if tri_2d.is_inside(p_2d):
            # projects onto triangle, so return difference in z
            return np.dot(np.linalg.inv(rot), np.array(list(p_2d) + [tri_rot_z]))
        else:
            closest_pt_2d = tri_2d.closest_point_to(p_2d)
            
            closest_pt_3d = np.array(list(closest_pt_2d) + [tri_rot_z])
            
            return np.dot(np.linalg.inv(rot), closest_pt_3d)
        
    def distance_to(self, p):
        """
        Find distance to point p
        by rotating and projecting
        
        :param p: 3d list or np.array
        :return float distance
        """
        closest_pt = self.closest_point_to(p)
        return np.linalg.norm(p - closest_pt)
    
    def area(self):
        """
        :return area of the triangle
        """
        tri_rot, rot = self.align_with([0,0,1])
        tri_2d = geometry2d.Triangle(tri_rot.a[:2], tri_rot.b[:2], tri_rot.c[:2])
        return tri_2d.area()
        
    def plot(self, sim, color=(1,0,0)):
        """
        :param sim: Simulator instance
        :param color: (r,g,b) [0,1]
        """
        sim.plot_segment(self.a, self.b, color)
        sim.plot_segment(self.b, self.c, color)
        sim.plot_segment(self.c, self.a, color)
        
        
#########
# TESTS #
#########
        
def test_align_with():
    t = Triangle([0,0,1.2], [0,1,1.2], [1,0,1.2])
    
    t_rot, rot = t.align_with([0,0,1])
    
    print('t_rot:\n{0}\n{1}\n{2}'.format(t_rot.a, t_rot.b, t_rot.c))
        
def test_distance_to():
    t = Triangle([0,0,0], [0,1,0], [1,0,0])
    
    p = [0, 0, 1]
    dist = t.distance_to(p)
    print('Distance should be 1')
    print('Computed distance is: {0}'.format(dist))
    
    p = [-1, 0, 1]
    dist = t.distance_to(p)
    print('Distance should be sqrt(2)')
    print('Computed distance is: {0}'.format(dist))
        
def test_distance_to_plot():
    from pr2_sim import simulator
    
    sim = simulator.Simulator(view=True)
    
    t = Triangle(np.random.rand(3), np.random.rand(3), np.random.rand(3))
    t.plot(sim)
    
    p = 2*np.random.rand(3)
    closest_pt = t.closest_point_to(p)
    
    sim.plot_point(p, color=(0,0,1))
    sim.plot_point(closest_pt)
    
    IPython.embed()
    
def test_beam_inside():
    from pr2_sim import simulator
    
    sim = simulator.Simulator(view=True)
    
    base = [.5,0,0]
    a = [.6, .1, .5]
    b = [.4, .1, .5]
    c = [.4, -.1, .5]
    d = [.6, -.1, .5]
    
    beam = Beam(base, a, b, c, d)
    beam.plot(sim)
    
    halfspaces = beam.get_halfspaces()
    for h in halfspaces:
        h.plot(sim)
        
    for i in xrange(1000):
        p = np.random.rand(3)
        is_inside = beam.is_inside(p)
        print('is_inside: {0}'.format(is_inside))
        sim.plot_point(p)
        if is_inside:
            raw_input()
        sim.clear_plots(1)
        
    
    IPython.embed()
        
if __name__ == '__main__':
    #test_align_with()
    #test_distance_to()
    #test_distance_to_plot()
    test_beam_inside()