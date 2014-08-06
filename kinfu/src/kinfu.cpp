#include "ros/ros.h"
#include <ros/console.h>

#include <tf/transform_listener.h>
#include <tf/transform_datatypes.h>
#include <tf_conversions/tf_eigen.h>
#include "sensor_msgs/PointCloud2.h"

#include <pcl_conversions.h>
// PCL specific includes
//#include <pcl/conversions.h>
#include <pcl/point_cloud.h>
#include <pcl/point_types.h>

#include <pcl/io/openni_grabber.h>
#include <pcl/io/pcd_io.h>

#include <Eigen/Eigen>
using namespace Eigen;

#include <boost/thread.hpp>

#include <pcl/console/parse.h>
#include <pcl/gpu/kinfu_large_scale/kinfu.h>
//#include <pcl/gpu/kinfu_large_scale/raycaster.h>
//#include <pcl/gpu/kinfu_large_scale/marching_cubes.h>
#include <pcl/gpu/kinfu_large_scale/tsdf_volume.h>
#include <pcl/gpu/containers/initialization.h>
#include <pcl/gpu/containers/device_array.h>

#include <cuda_runtime.h>
#include <assert.h>

typedef float VoxelT;
typedef short WeightT;

#define WIDTH_FULL   640 // 256
#define HEIGHT_FULL    480 // 192

#define W_SUB 64 // 64
#define H_SUB 48 // 48
#define N_SUB (W_SUB*H_SUB)

//#define USE_COLOR

boost::shared_ptr<tf::TransformListener> listener;

ros::Publisher pub, current_pointcloud_pub, variable_pub;
bool downloading;
int counter;
bool publish_kinfu_under_cam_depth_reg;

namespace carmine {
	const int WIDTH = 640;
	const int HEIGHT = 480;

	// https://vision.in.tum.de/data/datasets/rgbd-dataset/file_formats
	const double fx = 525.0;
	const double fy = 525.0;
	const double cx = 319.5;
	const double cy = 239.5;
}

namespace kinfu {
	// x and y must be equal!
	const double x = 2;
	const double y = 2;
	const double z = 2;

	const float shifting_distance = 5.0f;
}

template<typename MergedT, typename PointT>
typename pcl::PointCloud<MergedT>::Ptr merge(const pcl::PointCloud<PointT>& points, const pcl::PointCloud<pcl::RGB>& colors)
{    
  typename pcl::PointCloud<MergedT>::Ptr merged_ptr(new pcl::PointCloud<MergedT>());

  pcl::copyPointCloud (points, *merged_ptr);      
  for (size_t i = 0; i < colors.size (); ++i)
    merged_ptr->points[i].rgba = colors.points[i].rgba;

  return merged_ptr;
}


pcl::PointCloud<pcl::PointXYZRGB> last_cloud;

void update_kinfu_loop(pcl::gpu::kinfuLS::KinfuTracker *pcl_kinfu_tracker) {
	while(ros::ok()) {
		if (!downloading) {
			std::cout << "Updating kinfu...\n";

			pcl::PointCloud<pcl::PointXYZRGB> cloud;
			pcl::copyPointCloud(last_cloud, cloud);

			// get the current location of the camera relative to the kinfu frame (see kinfu.launch)
			tf::StampedTransform kinfu_to_camera;
			listener->waitForTransform("/kinfu_frame", "/camera_rgb_optical_frame",
					ros::Time(0), ros::Duration(5));
			listener->lookupTransform("/kinfu_frame", "/camera_rgb_optical_frame",
					ros::Time(0), kinfu_to_camera);

			// convert camera pose to format suitable for kinfu
			Affine3d affine_current_cam_pose;
			tf::transformTFToEigen(kinfu_to_camera, affine_current_cam_pose);

			pcl::PointCloud<pcl::PointXYZRGB> transformed_cloud;
			pcl::transformPointCloud(cloud, transformed_cloud, affine_current_cam_pose);


			// convert the data into gpu format for kinfu tracker to use
			pcl::gpu::DeviceArray2D<unsigned short> depth(carmine::HEIGHT,carmine::WIDTH);
			std::vector<unsigned short> data(carmine::HEIGHT*carmine::WIDTH);
			
			#ifdef USE_COLOR
			// the same for color data
			pcl::gpu::DeviceArray2D<pcl::gpu::kinfuLS::PixelRGB> color(carmine::HEIGHT,carmine::WIDTH);
			std::vector<pcl::gpu::kinfuLS::PixelRGB> color_data(carmine::HEIGHT*carmine::WIDTH);
			#endif

			const int cols = carmine::WIDTH;

			// TODO: Greg - does this really iterate through the points in the correct order?
			int i;
			pcl::PointCloud<pcl::PointXYZRGB>::iterator cloud_iter;
			for(cloud_iter = cloud.begin(), i = 0;
					cloud_iter != cloud.end();
					cloud_iter++, i++) {
				data[i] = static_cast<unsigned short>(1e3*cloud_iter->z);
				#ifdef USE_COLOR
				pcl::gpu::kinfuLS::PixelRGB current_pixel = pcl::gpu::kinfuLS::PixelRGB();
				current_pixel.r = cloud_iter->r;
				current_pixel.g = cloud_iter->g;
				current_pixel.b = cloud_iter->b;
				color_data[i] = current_pixel; 
				#endif
//				std::cout << cloud_iter->z << "\n";
			}
//			std::cout << "Cloud size: " << i << "\n";

//			for(cloud_iter = transformed_cloud.begin(), i = 0;
//					cloud_iter != transformed_cloud.end();
//					cloud_iter++, i++) {
//				std::cout << "(" << cloud_iter->x << ", " << cloud_iter->y << ", " << cloud_iter->z << ")\n";
//				if ((cloud_iter->x < 0) || (cloud_iter->y < 0) || (cloud_iter->z < 0)) {
//					std::cout << "crap: " << i << "\n";
//				}
//			}

			std::cout << "affined_current_cam_pose:\n" << affine_current_cam_pose.translation().transpose() << "\n";
			std::cout << affine_current_cam_pose.rotation() << "\n\n";

			depth.upload(data, cols);
			#ifdef USE_COLOR
			color.upload(color_data, cols);
			#endif

			// update kinfu tracker with new depth map and camera pose
			(*pcl_kinfu_tracker)(depth, affine_current_cam_pose.cast<float>());
			#ifdef USE_COLOR
			(*pcl_kinfu_tracker)(depth, color);
			#endif
			std::cout << "Updated kinfu!\n\n";
		}

		ros::spinOnce();
	}
}

void _cloud_callback (const pcl::PointCloud<pcl::PointXYZRGB>::ConstPtr& input) {
	pcl::copyPointCloud(*input, last_cloud);

//	int i;
//	pcl::PointCloud<pcl::PointXYZRGB>::iterator cloud_iter;
//	for(cloud_iter = last_cloud.begin(), i = 0;
//			cloud_iter != last_cloud.end();
//			cloud_iter++, i++) {
//		std::cout << cloud_iter->z << "\n";
//	}


	sensor_msgs::PointCloud2 output;
	toROSMsg(*input, output);

	output.header.stamp = ros::Time::now();
	output.header.frame_id = "/camera_rgb_optical_frame";

	// Publish the data
	current_pointcloud_pub.publish (output);
	if (!publish_kinfu_under_cam_depth_reg) {
	  variable_pub.publish(output);
	}

//	std::cout << "is organized: " << input->isOrganized() << "\n";
//	std::cout << "height: " << input->height << "\n";
//	std::cout << "width: " << input->width << "\n";
//
//	std::cout << "entered callback\n";
//	if (!downloading && (counter % 30 == 0)) {
//
//
//
//		std::cout << "updating" << "\n";
//		// get the current location of the camera relative to the kinfu frame
//		tf::StampedTransform kinfu_to_camera;
//		// published in kinfu.launch
//		listener->waitForTransform("/kinfu_frame", "/camera_rgb_optical_frame",
//				ros::Time(0), ros::Duration(5));
//		listener->lookupTransform("/kinfu_frame", "/camera_rgb_optical_frame",
//				ros::Time(0), kinfu_to_camera);
//
//		// convert camera pose to format suitable for kinfu
//		Affine3d affine_current_cam_pose;
//		tf::transformTFToEigen(kinfu_to_camera, affine_current_cam_pose);
//
//		pcl::PointCloud<pcl::PointXYZRGB> transformed_cloud;
//		pcl::transformPointCloud(*input, transformed_cloud, affine_current_cam_pose.inverse()); // might not need inverse here
//
//
//		// convert the data into gpu format for kinfu tracker to use
//		pcl::gpu::DeviceArray2D<unsigned short> depth(H_SUB,W_SUB);
//		std::vector<unsigned short> data(HEIGHT_FULL*WIDTH_FULL);
//		int cols = WIDTH_FULL;
//
//		int i;
//		pcl::PointCloud<pcl::PointXYZRGB>::iterator cloud_iter;
//		for(cloud_iter = transformed_cloud.begin(), i = 0;
//				cloud_iter != transformed_cloud.end();
//				cloud_iter++, i++) {
//			data[i] = static_cast<unsigned short>(cloud_iter->z);
//		}
//
//		depth.upload(data, cols);
//
//		// update kinfu tracker with new depth map and camera pose
//		(*pcl_kinfu_tracker)(depth, affine_current_cam_pose.cast<float>());
//		std::cout << "updated" << "\n";
//
//		//ros::spinOnce();
//	}
//	counter++;
}

pcl::gpu::kinfuLS::KinfuTracker* init_kinfu() {
	// setup kinfu tracker
	pcl::gpu::kinfuLS::KinfuTracker *pcl_kinfu_tracker = new pcl::gpu::kinfuLS::KinfuTracker(Vector3f(kinfu::y, kinfu::z, kinfu::x), kinfu::shifting_distance, carmine::HEIGHT, carmine::WIDTH);
	pcl_kinfu_tracker->setDepthIntrinsics(carmine::fx, carmine::fy, carmine::cx, carmine::cy);

	// the transform from /base_link to /kinfu_frame
	// now published in kinfu.launch, so the tf listener can just read it in
	tf::StampedTransform kinfu_to_camera;
	listener->waitForTransform("/kinfu_frame", "/camera_rgb_optical_frame",
			ros::Time(0), ros::Duration(5));
	listener->lookupTransform("/kinfu_frame", "/camera_rgb_optical_frame",
			ros::Time(0), kinfu_to_camera);

	// set initial pose in kinfu_tracker
	Affine3d affine_init_cam_pose;
	tf::transformTFToEigen(kinfu_to_camera, affine_init_cam_pose);
	pcl_kinfu_tracker->setInitialCameraPose(affine_init_cam_pose.cast<float>());
	pcl_kinfu_tracker->reset();
	#ifdef USE_COLOR
	pcl_kinfu_tracker->initColorIntegration();
	#endif

	return pcl_kinfu_tracker;
}

int main (int argc, char** argv) {
	pcl::console::setVerbosityLevel(pcl::console::L_ERROR);

	// Initialize ROS

	ros::init(argc, argv, "kinfu");
	//ros::Duration(2).sleep();
	ros::NodeHandle nh("~");
	std::string dev;
	// fill in tf listener
	listener.reset(new tf::TransformListener());

	if (!nh.getParam("device_number", dev)) {
		dev = "1";
	}
	std::string pointcloud_topic;
	if (!nh.getParam("pointcloud_topic", pointcloud_topic)) {
		pointcloud_topic = "kinfu_points";
	}
	std::cout << "pointcloud_topic: " << pointcloud_topic << "\n";

	// grabber to get data from the device
	pcl::Grabber* grabber = new pcl::OpenNIGrabber("#" + dev);

	// Create a ROS publisher for the output point cloud
	pub = nh.advertise<sensor_msgs::PointCloud2> (pointcloud_topic, 1);
	current_pointcloud_pub = nh.advertise<sensor_msgs::PointCloud2> ("camera_points", 1);
	boost::function<void (const pcl::PointCloud<pcl::PointXYZRGB>::ConstPtr&)> f = boost::bind(&_cloud_callback, _1);

	std::string variable_topic;
	if (!nh.getParam("handle_points_source", variable_topic)) {
	  publish_kinfu_under_cam_depth_reg = true;
	} else if (variable_topic == "kinfu") {
	  publish_kinfu_under_cam_depth_reg = true;
	} else {
	    publish_kinfu_under_cam_depth_reg = false;
	}

	variable_pub = nh.advertise<sensor_msgs::PointCloud2> ("/camera/depth_registered/points", 1);

	grabber->registerCallback(f);
	grabber->start();

	pcl::gpu::kinfuLS::KinfuTracker *pcl_kinfu_tracker = init_kinfu();

	ros::spinOnce();

	boost::thread update_kinfu_thread(update_kinfu_loop, pcl_kinfu_tracker);

	std::cout << "Ready to publish clouds\n";
	downloading = false;

	while(ros::ok()) {
		ros::spinOnce();
		ros::Duration(5).sleep();
//		std::string response;
//		std::getline(std::cin, response); // wait for key press
//		if (response == "q") {
//			grabber->stop();
//			pub.publish(output);
//			pcl::io::savePCDFileASCII("kinfu.pcd", current_cloud);
//			exit(0);
//		}

		downloading = true;
		std::cout << "Publishing kinfu cloud...\n";
		#ifndef USE_COLOR
		pcl::PointCloud<pcl::PointXYZ> current_cloud;
		// Download tsdf and convert to pointcloud
		pcl::gpu::kinfuLS::TsdfVolume tsdf = pcl_kinfu_tracker->volume();
		tsdf.fetchCloudHost(current_cloud);

// 		int i;
// 		pcl::PointCloud<pcl::PointXYZ>::iterator cloud_iter;
// 		for(cloud_iter = current_cloud.begin(), i = 0;
// 				cloud_iter != current_cloud.end();
// 				cloud_iter++, i++) {
// //			std::cout << "(" << cloud_iter->x << ", " << cloud_iter->y << ", " << cloud_iter->z << ")\n";
// 		}
// 		std::cout << "number iterated through: " << i << "\n";


		#else
		// new way of doing it, with color

		pcl::gpu::DeviceArray<pcl::PointXYZ> cloud_buffer_device_;
		pcl::gpu::DeviceArray<pcl::RGB> point_colors_device_;

		pcl::PointCloud<pcl::PointXYZ>::Ptr cloud_ptr_ = pcl::PointCloud<pcl::PointXYZ>::Ptr (new pcl::PointCloud<pcl::PointXYZ>);
		pcl::PointCloud<pcl::RGB>::Ptr point_colors_ptr_ = pcl::PointCloud<pcl::RGB>::Ptr (new pcl::PointCloud<pcl::RGB>);

		pcl::gpu::DeviceArray<pcl::PointXYZ> extracted = pcl_kinfu_tracker->volume().fetchCloud(cloud_buffer_device_); // cloud_buffer_device_ is just another pcl::gpu::DeviceArray<pcl::PointXYZ>
		extracted.download (cloud_ptr_->points);
		cloud_ptr_->width = (int)cloud_ptr_->points.size ();
		cloud_ptr_->height = 1;

		pcl_kinfu_tracker->colorVolume().fetchColors(extracted, point_colors_device_); // same as above for point_colors_device
		point_colors_device_.download(point_colors_ptr_->points);
		point_colors_ptr_->width = (int)point_colors_ptr_->points.size ();
		point_colors_ptr_->height = 1;


		pcl::PointCloud<pcl::PointXYZRGB> current_cloud = *(merge<pcl::PointXYZRGB>(*cloud_ptr_, *point_colors_ptr_));
		
		#endif

		//

		// get the transform from from rgb optical frame to kinfu frame
		tf::StampedTransform rgb_to_kinfu;
		listener->waitForTransform("/camera_rgb_optical_frame", "/kinfu_frame",
					   ros::Time(0), ros::Duration(5));
		listener->lookupTransform("/camera_rgb_optical_frame", "/kinfu_frame",
					  ros::Time(0), rgb_to_kinfu);

		// transform kinfu points back to rgb optical frame
		Affine3d current_transform;
		tf::transformTFToEigen(rgb_to_kinfu, current_transform);
		
		#ifdef USE_COLOR
		pcl::PointCloud<pcl::PointXYZRGB> transformed_cloud;
                #else
		pcl::PointCloud<pcl::PointXYZ> transformed_cloud;
		#endif
		
		pcl::transformPointCloud(current_cloud, transformed_cloud, current_transform);
		

		// Publish the data
		sensor_msgs::PointCloud2 output;
		toROSMsg(transformed_cloud, output);
		output.header.stamp = ros::Time::now();
		output.header.frame_id = "/camera_rgb_optical_frame";
		pub.publish (output);
		if (publish_kinfu_under_cam_depth_reg) {
		  variable_pub.publish(output);
		}
		downloading = false;
		std::cout << "Kinfu cloud published\n\n";

		std::cout << "output data: " << output.data.size() << "\n";

	}


	// Spin
	ros::spin ();
	grabber->stop();

	update_kinfu_thread.join();
}



/*
  pcl::gpu::DeviceArray<pcl::PointXYZ> cloud_buffer_device_;
  pcl::gpu::DeviceArray<pcl::RGB> point_colors_device_;

  pcl::PointCloud<pcl::PointXYZ>::Ptr cloud_ptr_ = pcl::PointCloud<pcl::PointXYZ>::Ptr (new pcl::PointCloud<pcl::PointXYZ>);
  pcl::PointCloud<pcl::RGB>::Ptr point_colors_ptr_ = pcl::PointCloud<pcl::RGB>::Ptr (new pcl::PointCloud<pcl::RGB>);

  pcl::gpu::DeviceArray<pcl::PointXYZ> extracted = kinfu.volume().fetchCloud (cloud_buffer_device_); // cloud_buffer_device_ is just another pcl::gpu::DeviceArray<pcl::PointXYZ>
  extracted.download (cloud_ptr_->points);
  cloud_ptr_->width = (int)cloud_ptr_->points.size ();
  cloud_ptr_->height = 1;

  kinfu.colorVolume().fetchColors(extracted, point_colors_device_); // same as above for point_colors_device
  point_colors_device_.download(point_colors_ptr_->points);
  point_colors_ptr_->width = (int)point_colors_ptr_->points.size ();
  point_colors_ptr_->height = 1;


  pcl::PointCloud<pcl::PointXYZRGB> current_cloud = std::merge<pcl::PointXYZRGB>(cloud_ptr_, point_colors_ptr_);

 */