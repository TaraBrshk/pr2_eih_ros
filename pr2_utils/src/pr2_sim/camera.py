import roslib
roslib.load_manifest('tfx')
import tfx

import numpy as np
from matplotlib import pyplot as plt
from matplotlib import delaunay

import time

from geometry import geometry2d, geometry3d
from pr2_sim import simulator, arm

# height=640., width=480., focal_length=.01, fx=480.*2., fy=640.*2., cx=480./2. + 0.5, cy=640./2. + 0.5
wrist_to_hand = tfx.transform((-0.106925711716, -0.00652973239027, -0.0566985277547),
                              (0.5013894362349414, 0.46636457585894514, 0.5099879560929402, 0.5206006149209386))

class Camera:
    def __init__(self, arm, sim, tool_to_camera=None,
                 height=480., width=640., focal_length=.01,
                 fx=525., fy=525., cx=319.5, cy=239.5, max_range=1.5):
                 #fx=640.*2., fy=480.*2., cx=640./2.+0.5, cy=480./2.+0.5, max_range=1.5):
        self.arm = arm
        self.sim = sim
        self.tool_to_camera = tool_to_camera if tool_to_camera is not None else tfx.transform(wrist_to_hand)
        
        self.height = height
        self.width = width
        
        self.fx, self.fy, self.cx, self.cy = fx, fy, cx, cy
        
        self.P = np.array([[fx,  0, cx],
                           [ 0, fy, cy],
                           [ 0,  0,  1]])
        
        self.focal_length = focal_length # TODO: not sure if needed
        self.max_range = 1.5
        self.height_m = focal_length*(height/fy)
        self.width_m = focal_length*(width/fx)
        
    ##############
    # state info #
    ##############
                
    def get_pose(self):
        """
        :return tfx.pose from frame 'base_link'
        """
        return tfx.pose(self.sim.transform_from_to(self.tool_to_camera.matrix, self.arm.tool_frame, 'base_link'), frame='base_link')
        
    #########################
    # camera matrix methods #
    #########################
        
    def pixel_from_point(self, point):
        """
        Projects point onto image plane
        
        :param point: tfx.pose/tfx.point
        :return 2d np.array (even if outside of image plane)
        """
        assert point.frame == 'base_link'
        
        cam_pose_mat = np.array(self.get_pose().as_tf().matrix)
        point_pose_mat = np.array(point.as_pose().matrix)
        point_transform_cam = np.linalg.solve(cam_pose_mat, point_pose_mat)
        #point_transform_cam = np.array(self.get_pose().as_tf()*point.as_pose())
        y = np.dot(self.P, point_transform_cam[:3,3])
        
        pixel = np.array([y[1]/float(y[2]), y[0]/float(y[2])])
        return pixel
        
    def segment_through_pixel(self, pixel):
        """
        Returns segment from camera origin through pixel
        
        :param pixel: 2d list/np.array
        :return geometry3d.Segment (with endpoints in frame 'base_link')
        """
        # assert (0 <= pixel[0] <= self.height) and (0 <= pixel[1] <= self.width)
        
        pixel = np.array(pixel)
        pixel_centered = pixel - np.array([self.height/2., self.width/2.])
        
        pixel3d_centered_m = self.max_range*np.array([pixel_centered[1]/self.fx,
                                                      pixel_centered[0]/self.fy,
                                                      1])
        
        transform = self.get_pose().as_tf()
        p0 = transform.position.array
        p1 = (transform*tfx.pose(pixel3d_centered_m)).position.array        
        
        return geometry3d.Segment(p0, p1)
    
    #####################
    # calculate frustum #
    #####################
    
    def truncated_view_frustum(self, triangles3d):
        """
        Truncates the view frustum against environment triangles
        
        :param triangles3d: list of geometry3d.Triangle with points in frame 'base_link'
        :return list of geometry3d.Pyramid
        """
        start_time = time.time()
        
        triangles2d = self.project_triangles(triangles3d)
        
        segments2d = [geometry2d.Segment([0,0],[self.height,0]),
                    geometry2d.Segment([self.height,0],[self.height,self.width]),
                    geometry2d.Segment([self.height,self.width],[0,self.width]),
                    geometry2d.Segment([0,self.width],[0,0])]
        for tri2d in triangles2d:
            segments2d += tri2d.segments()
        
        # get all segment intersections
        # and filter out those outside of image plane
        intersections = list()
        for i in xrange(len(segments2d)-1):
            curr_segment = segments2d[i]
            for j in xrange(i+1, len(segments2d)):
                 other_segment = segments2d[j]
                 intersection = curr_segment.intersection(other_segment)
                 if intersection is not None and (0 <= intersection[0] <= self.height) and (0 <= intersection[1] <= self.width):
                     intersections.append(intersection)
                   
        # perform delaunay triangulation with the intersections
        # keep looping until all delaunay edges only intersect
        # triangles2d at endpoints  
        while True:
            x = [p[0] for p in intersections]
            y = [p[1] for p in intersections]
            circumcenters, edges, tri_points, tri_neighbors = delaunay.delaunay(x,y)
            
            last_num_intersections = len(intersections)
            delaunay_segments2d = [geometry2d.Segment(intersections[indices[0]], intersections[indices[1]]) for indices in edges]
            for dseg2d in delaunay_segments2d:
                for seg2d in segments2d:
                    intersection = dseg2d.intersection(seg2d)
                    if intersection is not None and not dseg2d.is_endpoint(intersection):
                        intersections.append(intersection)
            
            if last_num_intersections == len(intersections):
                break
            
        delaunay_triangles2d = list()
        for indices in tri_points:
            p0 = intersections[indices[0]]
            p1 = intersections[indices[1]]
            p2 = intersections[indices[2]]
            delaunay_triangles2d.append(geometry2d.Triangle(p0, p1, p2))
            
        # for each delaunay triangle center
        # find the closest triangle3d that intersects
        # and then form a pyramid from the intersection points
        
        pyramids = list()
        camera_position = self.get_pose().position.array
        for dtri2d in delaunay_triangles2d:
            center = (dtri2d.a + dtri2d.b + dtri2d.c)/3.
            segment = self.segment_through_pixel(center)
            
            min_dist, min_tri3d = np.inf, None
            for tri3d in triangles3d:
                intersection = tri3d.intersection(segment)
                if intersection is not None:
                    dist = np.linalg.norm(intersection - camera_position)
                    if dist < min_dist:
                        min_dist = dist
                        min_tri3d = tri3d
                
            dtri3d_seg0 = self.segment_through_pixel(dtri2d.a)
            dtri3d_seg1 = self.segment_through_pixel(dtri2d.b)
            dtri3d_seg2 = self.segment_through_pixel(dtri2d.c)
                                    
            if min_tri3d is None:
                # no intersections, so max length
                pyramids.append(geometry3d.Pyramid(camera_position, dtri3d_seg0.p1, dtri3d_seg1.p1, dtri3d_seg2.p1))
            else:
                hyperplane3d = tri3d.hyperplane()
                dtri3d_intersection0 = hyperplane3d.intersection(dtri3d_seg0)
                dtri3d_intersection1 = hyperplane3d.intersection(dtri3d_seg1)
                dtri3d_intersection2 = hyperplane3d.intersection(dtri3d_seg2)
                assert dtri3d_intersection0 is not None
                assert dtri3d_intersection1 is not None
                assert dtri3d_intersection2 is not None
                pyramids.append(geometry3d.Pyramid(camera_position,
                                                   dtri3d_intersection0, dtri3d_intersection1, dtri3d_intersection2))
            
        print('truncated_view_frustum time: {0}'.format(time.time() - start_time))
                     
        fig = plt.figure()
        axes = fig.add_subplot(111)
        
        for segment in segments2d:
            p0, p1 = segment.p0, segment.p1
            p0_flip = [p0[1], self.height - p0[0]]
            p1_flip = [p1[1], self.height - p1[0]]
            axes.plot([p0_flip[0], p1_flip[0]], [p0_flip[1], p1_flip[1]], 'b--o', linewidth=3.0)
            #geometry2d.Segment(p0_flip,p1_flip).plot(axes, color='b')
        
        for intersection in intersections:
            axes.plot(intersection[1], self.height - intersection[0], 'rx', markersize=10.0)
        
        for t in delaunay_triangles2d:
            a, b, c = t.a, t.b, t.c
            a_flip = [a[1], self.height - a[0]]
            b_flip = [b[1], self.height - b[0]]
            c_flip = [c[1], self.height - c[0]]
            geometry2d.Triangle(a_flip,b_flip,c_flip).plot(axes, color='g')
            
        plt.show(block=False)
        
        for tri3d in triangles3d:
            geometry3d.Triangle(self.sim.transform_from_to(tfx.pose(tri3d.a).matrix,'base_link','world')[:3,3],
                                self.sim.transform_from_to(tfx.pose(tri3d.b).matrix,'base_link','world')[:3,3],
                                self.sim.transform_from_to(tfx.pose(tri3d.c).matrix,'base_link','world')[:3,3]).plot(self.sim, color=(1,0,0))
            #tri3d.plot(self.sim, color=(1,0,0))
        
        for pyramid in pyramids:
            geometry3d.Pyramid(self.sim.transform_from_to(tfx.pose(pyramid.base).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.a).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.b).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.c).matrix,'base_link','world')[:3,3]).plot(self.sim, with_sides=True, color=(0,1,0))
            #pyramid.plot(self.sim, with_sides=True, color=(0,1,0))
        
    def project_triangles(self, triangles3d):
        """
        Projects 3d triangles onto image plane and returns 2d triangles
        
        :param triangles3d: list of geometry3d.Triangle with points in frame 'base_link'
        :return list of geometry2d.Triangle with points in frame 'base_link'
        """
        triangles2d = list()
        for triangle3d in triangles3d:
            a_proj = self.pixel_from_point(tfx.point(triangle3d.a, frame='base_link'))
            b_proj = self.pixel_from_point(tfx.point(triangle3d.b, frame='base_link'))
            c_proj = self.pixel_from_point(tfx.point(triangle3d.c, frame='base_link'))
            
            triangles2d.append(geometry2d.Triangle(a_proj, b_proj, c_proj))
            
        return triangles2d
    
    def truncated_view_frustum_new(self, triangles3d):
        """
        Truncates the view frustum against environment triangles
        
        :param triangles3d: list of geometry3d.Triangle with points in frame 'base_link'
        :return list of geometry3d.Pyramid
        """
        start_time = time.time()
        
        frustum = geometry3d.RectangularPyramid(self.get_pose().position.array,
                                                self.segment_through_pixel([0,self.width]).p1,
                                                self.segment_through_pixel([0,0]).p1,
                                                self.segment_through_pixel([self.height,0]).p1,
                                                self.segment_through_pixel([self.height,self.width]).p1)
        
#         print('Number of triangles3d: {0}'.format(len(triangles3d)))
        clipped_triangles3d = list()
        for tri3d in triangles3d:
            clipped_triangles3d += frustum.clip_triangle(tri3d)
#         print('Number of clipped triangles: {0}'.format(len(clipped_triangles3d)))
            
        triangles2d = self.project_triangles(clipped_triangles3d)
        
        segments2d = set()
        for tri2d in triangles2d:
            segments2d.update(tri2d.segments())
        
        # avoid redundant points
        points2d = {geometry3d.Point([0,0]),
                    geometry3d.Point([self.height,0]),
                    geometry3d.Point([0,self.width]),
                    geometry3d.Point([self.height,self.width])}
        # add vertices
        for seg2d in segments2d:
            points2d.add(geometry3d.Point(seg2d.p0))
            points2d.add(geometry3d.Point(seg2d.p1))
        # add intersections
        segments2d_list = list(segments2d)
        for i in xrange(len(segments2d_list)-1):
            seg2d = segments2d_list[i]
            for j in xrange(i+1, len(segments2d_list)):
                other_seg2d = segments2d_list[j]
                intersection = seg2d.intersection(other_seg2d)
                if intersection is not None:
                    points2d.add(geometry3d.Point(intersection))
                    
        partition_triangles2d = set()
        for pt2d in points2d:
#             print('len(segments2d): {0}'.format(len(segments2d)))
            
            # find other points that don't cross anything in segment2d
            p = pt2d.p
            points_in_los = set()
            for other_pt2d in points2d:
                other_p = other_pt2d.p
                if pt2d != other_pt2d:
                    seg2d = geometry2d.Segment(p, other_p)
                    for check_seg2d in segments2d:
                        if not seg2d.is_parallel(check_seg2d):
                            intersection = seg2d.intersection(check_seg2d)
                            if intersection is not None and not seg2d.is_endpoint(intersection):
                                break
                    else:
                        points_in_los.add(geometry2d.Point(other_p)) 
                                
#             points_in_los = filter(lambda x: 0 <= x.p[0] <= self.height and 0 <= x.p[1] <= self.width, points_in_los)
                                
#             print('Number in line of sight of ({0}, {1}): {2}'.format(p[1], self.height-p[0], len(points_in_los)))
#             for point_in_los in points_in_los:
#                 print('({0}, {1})'.format(point_in_los.p[1], self.height-point_in_los.p[0]))
            
            # sort segments by angle
            seg2d_compare = geometry2d.Segment([0,0], [-1,0])
            segments2d_in_los_sorted = sorted([geometry2d.Segment(p, other.p) for other in points_in_los], key=lambda seg: seg.angle(seg2d_compare))
#             for s in segments2d_in_los_sorted:
#                 print('({0},{1})\t({2},{3})'.format(s.p0[1], self.height - s.p0[0], s.p1[1], self.height - s.p1[0]))
                
            #new_partition_triangles2d = [geometry2d.Triangle(p, segments2d_in_los_sorted[i].p1, segments2d_in_los_sorted[i+1].p1) for i in xrange(len(segments2d_in_los_sorted)-1)]
            new_partition_triangles2d = set()
            for i in xrange(len(segments2d_in_los_sorted)-1):
                tri2d = geometry2d.Triangle(p, segments2d_in_los_sorted[i].p1, segments2d_in_los_sorted[i+1].p1)
                not_colliding = True
                for tri_seg2d in tri2d.segments():
                    for check_seg2d in segments2d:
                        if not tri_seg2d.is_parallel(check_seg2d):
                            intersection = tri_seg2d.intersection(check_seg2d)
                            if intersection is not None and not tri_seg2d.is_endpoint(intersection):
                                not_colliding = False
                                break
                    if not_colliding is False:
                        break
                
                if not_colliding:        
                    new_partition_triangles2d.add(tri2d)
            
            # update partition and new segments
            partition_triangles2d.update(new_partition_triangles2d)
            for tri2d in new_partition_triangles2d:
                segments2d.update(tri2d.segments())
                
            total_area = sum([tri2d.area() for tri2d in partition_triangles2d])
            if total_area >= self.width*self.height:
                break
            
        
        camera_position = self.get_pose().position.array
        pyramids3d = list()
        for tri2d in partition_triangles2d:
            tri2d_vertices = tri2d.vertices()
            center2d = sum(tri2d_vertices)/3.
            
            center_seg3d = self.segment_through_pixel(center2d)
            vertices_seg3d = [self.segment_through_pixel(vertex) for vertex in tri2d_vertices]
            
            min_dist, min_tri3d = np.inf, None
            for tri3d in triangles3d:
                intersection = tri3d.intersection(center_seg3d)
                if intersection is not None:
                    dist = np.linalg.norm(intersection - camera_position)
                    if dist < min_dist:
                        min_dist = dist
                        min_tri3d = tri3d
                    
            if min_tri3d is not None:
                hyperplane3d = min_tri3d.hyperplane()
                tri3d_intersections = [hyperplane3d.intersection(vertex_seg3d) for vertex_seg3d in vertices_seg3d]
                assert len(filter(lambda x: x is None, tri3d_intersections)) == 0
                pyramids3d.append(geometry3d.Pyramid(camera_position, tri3d_intersections[0], tri3d_intersections[1], tri3d_intersections[2]))
            else:
                pyramids3d.append(geometry3d.Pyramid(camera_position, vertices_seg3d[0].p1, vertices_seg3d[1].p1, vertices_seg3d[2].p1))
            
                
        
        
        print('Total time: {0}'.format(time.time() - start_time))
        total_area = sum([tri2d.area() for tri2d in partition_triangles2d])
        print('Total area (should be {0}): {1}'.format(self.width*self.height, total_area))
            
        #################                    
        # TEMP plotting #
        #################
        self.plot()
        for tri3d in triangles3d:
            geometry3d.Triangle(self.sim.transform_from_to(tfx.pose(tri3d.a).matrix,'base_link','world')[:3,3],
                                self.sim.transform_from_to(tfx.pose(tri3d.b).matrix,'base_link','world')[:3,3],
                                self.sim.transform_from_to(tfx.pose(tri3d.c).matrix,'base_link','world')[:3,3]).plot(self.sim, color=(1,0,0))
            
            
        raw_input()
        for pyramid in pyramids3d:
            geometry3d.Pyramid(self.sim.transform_from_to(tfx.pose(pyramid.base).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.a).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.b).matrix,'base_link','world')[:3,3],
                               self.sim.transform_from_to(tfx.pose(pyramid.c).matrix,'base_link','world')[:3,3]).plot(self.sim, with_sides=False, color=(0,1,0))
            
        fig = plt.figure()
        axes = fig.add_subplot(111)
        
        for tri2d in triangles2d:
            for segment in tri2d.segments():
                p0, p1 = segment.p0, segment.p1
                p0_flip = [p0[1], self.height - p0[0]]
                p1_flip = [p1[1], self.height - p1[0]]
                axes.plot([p0_flip[0], p1_flip[0]], [p0_flip[1], p1_flip[1]], 'b--o', linewidth=2.0)
             
        axes.plot([0, 0, self.width, self.width, 0], [0, self.height, self.height, 0, 0], 'b--o', linewidth=2.0)
        
        for pt2d in points2d:
            axes.plot(pt2d.p[1], self.height - pt2d.p[0], 'rx', markersize=10.0)
            
        plt.xlim((-10, self.width+10))
        plt.ylim((-10, self.height+10))
                
        plt.show(block=False)
        
        colors = plt.cm.hsv(np.linspace(0, 1, len(partition_triangles2d)))
        for i, tri2d in enumerate(partition_triangles2d):
            x = [p[1] for p in tri2d.vertices()]
            y = [self.height - p[0] for p in tri2d.vertices()]
            axes.fill(x, y, color=colors[i])
        
            plt.show(block=False)
#             raw_input()
        #################
                                
    
    ##################
    # visualizations #
    ##################
        
    def plot(self, color=(1,0,0)):
        """
        :param color: (r,g,b) [0,1]
        """
        origin = self.get_pose().position.array
        side_segments = [self.segment_through_pixel([0., 0.]),
                    self.segment_through_pixel([self.height, 0.]),
                    self.segment_through_pixel([self.height, self.width]),
                    self.segment_through_pixel([0., self.width])]
        
        end_points = [segment.p1 for segment in side_segments+[side_segments[0]]]
        end_segments = [geometry3d.Segment(end_points[i], end_points[i+1]) for i in xrange(len(end_points)-1)]
        
        for segment in side_segments+end_segments:
            p0_world = self.sim.transform_from_to(tfx.pose(segment.p0).matrix, 'base_link', 'world')[:3,3]
            p1_world = self.sim.transform_from_to(tfx.pose(segment.p1).matrix, 'base_link', 'world')[:3,3]
            geometry3d.Segment(p0_world, p1_world).plot(self.sim, color=color)
            
            
###########
#  TESTS  #
###########

def test_truncated_view_frustum():
    sim = simulator.Simulator(view=True)
    larm = arm.Arm('left',sim=sim)
    larm.set_posture('mantis')
    rarm = arm.Arm('right',sim=sim)
    rarm.set_posture('mantis')
    
    cam = Camera(rarm, sim)
#     triangles3d = [geometry3d.Triangle([.7,0,.8],[.7,0,1.1],[.7,-.3,.7])]
#     triangles3d = [geometry3d.Triangle([.5,0,.5],[.8,0,.6],[.5,-.3,.9])]
#     triangles3d = [geometry3d.Triangle([np.random.uniform(.2,.5), np.random.uniform(-.5,0), np.random.uniform(.25,.75)],
#                                        [np.random.uniform(.2,.5), np.random.uniform(-.5,0), np.random.uniform(.25,.75)],
#                                        [np.random.uniform(.2,.5), np.random.uniform(-.5,0), np.random.uniform(.25,.75)]) for _ in xrange(3)]
    table_center = np.array([.2,.7,.5])
    triangles3d = [geometry3d.Triangle(table_center, table_center+np.array([.5,-1.4,0]), table_center+np.array([.5,0,0])),
                   geometry3d.Triangle(table_center, table_center+np.array([0,-1.4,0]), table_center+np.array([.5,-1.4,0])),
                   geometry3d.Triangle(table_center+np.array([.25,-.7,0]), table_center+np.array([.25,-.7,.2]), table_center+np.array([.25,-.9,0]))]
    cam.truncated_view_frustum_new(triangles3d)
    
    print('Press enter to exit')
    raw_input()
    

def test_project_triangles():
    sim = simulator.Simulator(view=True)
    rarm = arm.Arm('right',sim=sim)
    rarm.set_posture('mantis')
    
    cam = Camera(rarm, sim)
    triangles3d = [geometry3d.Triangle([.7,0,.8],[.7,0,1.1],[.7,-.3,.7])]
#     triangles3d = [geometry3d.Triangle(cam.segment_through_pixel([1,1]).p1,
#                                        cam.segment_through_pixel([cam.height-1,cam.width-1]).p1,
#                                        cam.segment_through_pixel([1,cam.width-1]).p1)]
    triangles2d = cam.project_triangles(triangles3d)
    
    for triangle3d in triangles3d:
        triangle3d.plot(sim)
    cam.plot()
    
    fig = plt.figure()
    axes = fig.add_subplot(111)
    
    for t in triangles2d:
        a, b, c = t.a, t.b, t.c
        a_flip = [a[1], cam.height - a[0]]
        b_flip = [b[1], cam.height - b[0]]
        c_flip = [c[1], cam.height - c[0]]
        geometry2d.Triangle(a_flip,b_flip,c_flip).plot(axes, color='b')
    
    plt.xlim((-1, cam.width+1))
    plt.ylim((-1, cam.height+1))
        
    plt.show(block=False)
        
    print('Press enter to exit')
    raw_input()
    

def test_camera_teleop():
    sim = simulator.Simulator(view=True)
    rarm = arm.Arm('right',sim=sim)
    rarm.set_posture('mantis')
    
    cam = Camera(rarm, sim)
    
    rarm.teleop()
    cam.plot(sim)
    
    print('Press enter to exit')
    raw_input()

if __name__ == '__main__':
    test_truncated_view_frustum()
    #test_project_triangles()
    #test_camera_teleop()
    